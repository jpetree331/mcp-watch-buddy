"""Frame bundler — collects 1 second of frames and produces an MCP-ready bundle.

Public API (used by tests):
    bundler = Bundler(on_bundle_ready=callback)
    bundler.start()
    bundler.feed(pil_image)
    bundler.stop()

The callback receives a dict:
    {
      "frames": [base64 PNG strings — full baseline on first call, changed regions after],
      "metadata": [{"is_baseline": bool} | {"frame_index": int, "changed_regions": [...], "cached_regions": [...]}],
      "timestamp": ISO UTC string,
      "window_title": str,
      "frame_count": int,
      "has_changes": bool,
      "is_baseline": bool   ← True on the very first bundle only
    }
"""

import base64
import io
import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

# Use absolute imports so module works both as package and standalone
try:
    from .delta import DeltaEngine, RegionChange
except ImportError:
    from delta import DeltaEngine, RegionChange  # type: ignore[no-reattr]


def _load_config() -> dict:
    p = Path(__file__).parent / "config.json"
    if p.exists():
        return json.loads(p.read_text())
    return {}


class Bundler:
    """Callback-based bundler: feed frames in, get 1-second bundles out."""

    def __init__(
        self,
        on_bundle_ready: Callable[[dict], None],
        bundle_interval: float = 0.0,
        delta_engine: Optional[DeltaEngine] = None,
        region: Optional[dict] = None,
        window_title: str = "",
    ):
        self.on_bundle_ready = on_bundle_ready
        cfg = _load_config()
        self.bundle_interval = bundle_interval or cfg.get("bundle_interval_seconds", 1.0)
        self.region = region or cfg.get("region", {"x": 0, "y": 0, "width": 1280, "height": 720})
        self.window_title = window_title
        self._delta = delta_engine or DeltaEngine()

        # Thread-safe queue; cap at 30 frames to prevent memory runaway
        self._q: queue.Queue = queue.Queue(maxsize=30)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._prev_frame: Optional[Image.Image] = None
        self._prev_frame_hash: str = ""
        self._baseline_sent: bool = False  # True after first full-frame snapshot

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def start(self):
        """Start the background bundle-flush thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

    def feed(self, frame: Image.Image):
        """Push one captured frame into the queue (non-blocking; drops oldest on overflow)."""
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            try:
                self._q.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(frame)
            except queue.Full:
                pass

    def stop(self):
        """Signal the flush loop to stop and wait for it."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    # ------------------------------------------------------------------
    # Internal flush loop
    # ------------------------------------------------------------------

    def _flush_loop(self):
        while not self._stop_event.is_set():
            deadline = time.monotonic() + self.bundle_interval
            # Collect frames until the interval elapses
            collected: list[Image.Image] = []
            while time.monotonic() < deadline and not self._stop_event.is_set():
                remaining = deadline - time.monotonic()
                try:
                    frame = self._q.get(timeout=min(0.05, max(0, remaining)))
                    collected.append(frame)
                except queue.Empty:
                    pass

            if not collected:
                continue

            bundle = self._build_bundle(collected)
            if bundle is not None:
                try:
                    self.on_bundle_ready(bundle)
                except Exception as e:
                    print(f"[bundler] on_bundle_ready error: {e}")

    def _build_bundle(self, frames: list[Image.Image]) -> Optional[dict]:
        """Process up to 10 frames, run delta detection, build bundle dict.

        Hash comparison gates whether a bundle is produced at all (catches subtle
        uniform shifts below the pixel threshold).  Contour detection provides
        fine-grained region metadata and crops for encoding.
        """
        # Cap at 10 frames per bundle
        frames = frames[-10:]

        encoded: list[str] = []
        metadata: list[dict] = []
        any_hash_change = False

        # Efficiency guards (added 2026-07-15 after live field test):
        # - sent_hashes: perceptual dedup — never ship two images in one
        #   bundle that downsample identically (video noise produced near-
        #   identical crops/thumbnails on every frame).
        # - uniform_thumb_sent: the "hash changed but contours sub-threshold"
        #   fallback used to emit a full 640x360 thumbnail PER FRAME — up to
        #   ten near-identical full pages per bundle. One is plenty.
        sent_hashes: set[str] = set()
        uniform_thumb_sent = False

        def _add_image(img: Image.Image) -> bool:
            key = _hash_frame(img)
            if key in sent_hashes:
                return False
            sent_hashes.add(key)
            encoded.append(_encode_png(img))
            return True

        # --- Baseline snapshot (first bundle only) ---
        # Claude needs to see the full frame once so it knows what static
        # regions (HUD, UI chrome, scoreboard, etc.) look like.
        # After this, only changed zone crops are sent.
        if not self._baseline_sent and frames:
            baseline = frames[0].copy()
            baseline.thumbnail((1280, 720), Image.Resampling.LANCZOS)
            _add_image(baseline)
            metadata.append({"is_baseline": True, "width": baseline.width, "height": baseline.height})
            self._baseline_sent = True

        for idx, frame in enumerate(frames):
            frame_hash = _hash_frame(frame)

            if self._prev_frame is None or frame_hash == self._prev_frame_hash:
                # No change detected via hash
                changed, cached = [], list(self._delta.zones.keys()) if self._delta.zones else []
            else:
                # Hash says something changed — get fine-grained regions from delta
                any_hash_change = True
                result = self._delta.process(self._prev_frame, frame)
                changed = result["changed_regions"]
                cached = result["cached_regions"]

                if changed:
                    # Encode precise changed-region crops (deduped perceptually)
                    for region in changed:
                        crop = self._safe_crop(frame, region)
                        if crop is not None:
                            _add_image(crop)
                else:
                    # Hash changed but contours below noise threshold (subtle
                    # uniform shift). One context thumbnail per bundle, max.
                    if not uniform_thumb_sent:
                        thumb = frame.copy()
                        thumb.thumbnail((640, 360))
                        if _add_image(thumb):
                            uniform_thumb_sent = True

            self._prev_frame = frame
            self._prev_frame_hash = frame_hash

            metadata.append({
                "frame_index": idx,
                "changed_regions": [
                    {"name": r.name, "x": r.x, "y": r.y, "w": r.w, "h": r.h}
                    for r in changed
                ],
                "cached_regions": [r if isinstance(r, str) else r.name for r in cached],
            })

        # A baseline bundle is always meaningful even if nothing moved yet
        is_baseline = len(metadata) > 0 and metadata[0].get("is_baseline", False)

        return {
            "frames": encoded,
            "metadata": metadata,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "window_title": self.window_title,
            "frame_count": len(frames),
            "has_changes": any_hash_change or is_baseline,
            "is_baseline": is_baseline,
        }

    def _safe_crop(self, frame: Image.Image, region: RegionChange) -> Optional[Image.Image]:
        fw, fh = frame.size
        rx = max(0, region.x - self.region.get("x", 0))
        ry = max(0, region.y - self.region.get("y", 0))
        rw = region.w
        rh = region.h
        if rx >= fw or ry >= fh or rw <= 0 or rh <= 0:
            return None
        return frame.crop((rx, ry, min(rx + rw, fw), min(ry + rh, fh)))


# ------------------------------------------------------------------
# FrameBundler — alias kept for server.py compatibility
# ------------------------------------------------------------------

class FrameBundler(Bundler):
    """Used by server.py pipeline — wraps Bundler with flush_bundle() interface."""

    def __init__(
        self,
        delta_engine: DeltaEngine,
        bundle_interval: float = 1.0,
        region: Optional[dict] = None,
        window_title: str = "",
    ):
        super().__init__(
            on_bundle_ready=lambda b: None,
            bundle_interval=bundle_interval,
            delta_engine=delta_engine,
            region=region,
            window_title=window_title,
        )
        self._last_bundle: Optional[dict] = None

    def ingest_frame(self, frame: Image.Image, timestamp: float):
        """Callback-compatible shim for ScreenCapture.start()."""
        self.feed(frame)

    def flush_bundle(self):
        """Drain queue immediately and return bundle (used by server bundle_loop)."""
        frames = []
        while True:
            try:
                frames.append(self._q.get_nowait())
            except queue.Empty:
                break
        if not frames:
            return _EmptyBundle()
        result = self._build_bundle(frames)
        if result is None:
            return _EmptyBundle()
        return _BundleWrapper(result)


class _EmptyBundle:
    frames: list = []
    metadata: list = []
    timestamp: str = ""
    window_title: str = ""
    frame_count: int = 0
    has_changes: bool = False
    def is_empty(self): return True
    def to_dict(self) -> dict:
        return {"has_changes": False, "frames": [], "metadata": [],
                "timestamp": self.timestamp, "window_title": "", "frame_count": 0}


class _BundleWrapper:
    def __init__(self, d: dict):
        self.frames = d["frames"]
        self.metadata = d["metadata"]
        self.timestamp = d["timestamp"]
        self.window_title = d.get("window_title", "")
        self.frame_count = d.get("frame_count", len(self.frames))
        self.has_changes = d.get("has_changes", bool(self.frames))
    def is_empty(self): return not self.has_changes
    def to_dict(self) -> dict:
        return {
            "frames": self.frames,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
            "window_title": self.window_title,
            "frame_count": self.frame_count,
            "has_changes": self.has_changes,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _encode_png(img: Image.Image) -> str:
    """PNG-encode a PIL image and return base64 string."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _hash_frame(img: Image.Image) -> str:
    """Fast hash of a downsampled frame for change detection."""
    import hashlib
    small = img.resize((64, 64), Image.Resampling.LANCZOS)
    return hashlib.md5(small.tobytes()).hexdigest()
