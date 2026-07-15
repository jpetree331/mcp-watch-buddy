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

        # v1.1 — Delta Atlas: pack all changed-region crops into ONE gutter-
        # separated mosaic per bundle (Jess's original "send only the pixels
        # that move" concept, adapted to vision's rectangle-shaped appetite).
        self.atlas_cfg = {
            **{"enabled": True, "gutter": 8, "max_tiles": 12, "max_width": 1024},
            **cfg.get("atlas", {}),
        }
        # v1.1 — content-aware crop downscaling. DEFAULT OFF. Text zones
        # (chat) are always excluded: text needs resolution; webcams don't.
        self.downscale_cfg = {
            **{"enabled": False, "min_dimension": 400, "scale": 0.6,
               "exclude_zones": ["chat"]},
            **cfg.get("crop_downscale", {}),
        }

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
        tiles: list[dict] = []  # changed-region crops awaiting atlas packing

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
                    # Collect deduped changed-region crops as atlas tiles
                    for region in changed:
                        crop = self._safe_crop(frame, region)
                        if crop is None:
                            continue
                        key = _hash_frame(crop)
                        if key in sent_hashes:
                            continue
                        sent_hashes.add(key)
                        tiles.append({
                            "frame_index": idx, "name": region.name,
                            "x": region.x, "y": region.y,
                            "w": region.w, "h": region.h, "img": crop,
                        })
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

        # Ship the collected tiles: packed into one atlas when there are
        # several, or as plain crops when there's just one (or atlas is off).
        atlas_legend = None
        if tiles:
            tiles = self._maybe_downscale(tiles)
            if self.atlas_cfg.get("enabled", True) and len(tiles) >= 2:
                atlas_img, atlas_legend = _pack_atlas(
                    tiles,
                    gutter=int(self.atlas_cfg.get("gutter", 8)),
                    max_tiles=int(self.atlas_cfg.get("max_tiles", 12)),
                    max_width=int(self.atlas_cfg.get("max_width", 1024)),
                )
                encoded.append(_encode_png(atlas_img))
            else:
                for t in tiles:
                    encoded.append(_encode_png(t["img"]))

        # A baseline bundle is always meaningful even if nothing moved yet
        is_baseline = len(metadata) > 0 and metadata[0].get("is_baseline", False)

        return {
            "frames": encoded,
            "metadata": metadata,
            "atlas": atlas_legend,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "window_title": self.window_title,
            "frame_count": len(frames),
            "has_changes": any_hash_change or is_baseline,
            "is_baseline": is_baseline,
        }

    def _maybe_downscale(self, tiles: list[dict]) -> list[dict]:
        """Content-aware downscale (opt-in): shrink large photographic crops;
        never touch tiles centered in excluded (text-heavy) zones."""
        cfg = self.downscale_cfg
        if not cfg.get("enabled", False):
            return tiles
        min_dim = int(cfg.get("min_dimension", 400))
        scale = float(cfg.get("scale", 0.6))
        excluded = [
            z for name, z in getattr(self._delta, "zones", {}).items()
            if name in cfg.get("exclude_zones", [])
        ]
        out = []
        for t in tiles:
            img = t["img"]
            cx, cy = t["x"] + t["w"] // 2, t["y"] + t["h"] // 2
            in_excluded = any(
                z.x <= cx < z.x + z.width and z.y <= cy < z.y + z.height
                for z in excluded
            )
            if not in_excluded and img.width > min_dim and img.height > min_dim:
                img = img.resize(
                    (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                t = {**t, "img": img, "downscaled": scale}
            out.append(t)
        return out

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

def _pack_atlas(
    tiles: list[dict],
    gutter: int = 8,
    max_tiles: int = 12,
    max_width: int = 1024,
    background: tuple = (96, 96, 96),
) -> tuple:
    """Pack changed-region crops into one gutter-separated mosaic.

    Ships only the pixels that moved, tiled densely — total canvas area is
    roughly the sum of the changes rather than one image per change. Gutters
    keep vision from blending adjacent tiles into a single hallucinated
    scene; the returned legend maps every tile back to its original screen
    coordinates ("src") and its position inside the atlas ("atlas").
    """
    dropped = 0
    if len(tiles) > max_tiles:
        dropped = len(tiles) - max_tiles
        tiles = tiles[-max_tiles:]  # keep the most recent — recency wins

    # Shelf packing: tallest tiles first for row density
    order = sorted(range(len(tiles)), key=lambda i: -tiles[i]["img"].height)
    row_limit = max(max_width, max(t["img"].width for t in tiles) + 2 * gutter)
    rows: list[list[int]] = []
    cur_row: list[int] = []
    cur_w = gutter
    for i in order:
        w = tiles[i]["img"].width
        if cur_row and cur_w + w + gutter > row_limit:
            rows.append(cur_row)
            cur_row, cur_w = [], gutter
        cur_row.append(i)
        cur_w += w + gutter
    if cur_row:
        rows.append(cur_row)

    positions: dict[int, tuple] = {}
    canvas_w, y = 0, gutter
    for row in rows:
        x = gutter
        row_h = max(tiles[i]["img"].height for i in row)
        for i in row:
            positions[i] = (x, y)
            x += tiles[i]["img"].width + gutter
        canvas_w = max(canvas_w, x)
        y += row_h + gutter

    canvas = Image.new("RGB", (canvas_w, y), background)
    legend = []
    for i, (px, py) in positions.items():
        t = tiles[i]
        canvas.paste(t["img"], (px, py))
        entry = {
            "tile": len(legend),
            "frame_index": t["frame_index"],
            "name": t["name"],
            "src": {"x": t["x"], "y": t["y"], "w": t["w"], "h": t["h"]},
            "atlas": {"x": px, "y": py, "w": t["img"].width, "h": t["img"].height},
        }
        if t.get("downscaled"):
            entry["downscaled"] = t["downscaled"]
        legend.append(entry)

    return canvas, {
        "tiles": legend,
        "gutter": gutter,
        "dropped_tiles": dropped,
        "note": ("One packed image contains every changed region, separated "
                 "by gray gutters; 'src' gives original screen coordinates."),
    }


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
