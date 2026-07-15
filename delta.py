"""Delta engine — detects meaningful screen changes between frames.

Public API (used by tests and bundler):
    engine = DeltaEngine()
    result = engine.process(frame_prev, frame_cur)
    # result = {"changed_regions": [RegionChange, ...], "cached_regions": [str, ...]}

Internal API (used by bundler pipeline):
    engine.process_frame(frame)  — single-frame variant that keeps prev internally
"""

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image


def _load_config() -> dict:
    p = Path(__file__).parent / "config.json"
    if p.exists():
        return json.loads(p.read_text())
    return {}


@dataclass
class RegionChange:
    name: str
    x: int
    y: int
    w: int
    h: int


@dataclass
class _ZoneState:
    x: int
    y: int
    width: int
    height: int
    last_hash: str = ""
    last_seen: float = 0.0
    changed: bool = False


class DeltaEngine:
    """Computes frame diffs and tracks per-zone change state.

    Can be used statelessly via process(prev, cur) or statelessly via
    process_frame(cur) which maintains internal prev-frame state.
    """

    def __init__(
        self,
        screen_zones: Optional[dict] = None,
        change_threshold: int = 0,
        min_contour_area: int = 0,
        frame_size: Optional[tuple] = None,
    ):
        cfg = _load_config()
        # Fall back to config values if not provided
        self.change_threshold = change_threshold or cfg.get("change_threshold", 25)
        self.min_contour_area = min_contour_area or cfg.get("min_contour_area", 500)

        zones_cfg = screen_zones or cfg.get("screen_zones", {})
        # Zones are authored in a design space (whatever layout the config
        # describes, e.g. 1280x720). When the actual captured frame size is
        # known, scale zones to fit it — otherwise zone caching silently
        # degrades on windows of any other size (found live 2026-07-15).
        if frame_size and zones_cfg:
            zones_cfg = self._scale_zones(zones_cfg, frame_size)
        self.zones: dict[str, _ZoneState] = {
            name: _ZoneState(
                x=z["x"], y=z["y"], width=z["width"], height=z["height"],
                last_seen=time.monotonic(),
            )
            for name, z in zones_cfg.items()
        }
        self._prev_frame: Optional[np.ndarray] = None

    @staticmethod
    def _scale_zones(zones_cfg: dict, frame_size: tuple) -> dict:
        """Scale zones from their design space to the actual frame size.

        The design space is inferred from the zones themselves (the bounding
        box they collectively cover), so any authored layout scales correctly.
        """
        design_w = max((z["x"] + z["width"]) for z in zones_cfg.values())
        design_h = max((z["y"] + z["height"]) for z in zones_cfg.values())
        fw, fh = frame_size
        if design_w <= 0 or design_h <= 0 or fw <= 0 or fh <= 0:
            return zones_cfg
        sx, sy = fw / design_w, fh / design_h
        return {
            name: {
                "x": round(z["x"] * sx),
                "y": round(z["y"] * sy),
                "width": round(z["width"] * sx),
                "height": round(z["height"] * sy),
            }
            for name, z in zones_cfg.items()
        }

    # ------------------------------------------------------------------
    # Two-frame stateless API (used by tests)
    # ------------------------------------------------------------------

    def process(
        self, frame_prev: Image.Image, frame_cur: Image.Image
    ) -> dict:
        """Compare two PIL frames; return changed and cached region lists.

        Returns:
            {
              "changed_regions": [RegionChange, ...],
              "cached_regions": [str, ...],   # zone names with no change
            }
        """
        cv_prev = self._to_cv(frame_prev)
        cv_cur = self._to_cv(frame_cur)
        changed, cached = self._compare(cv_prev, cv_cur)
        self._prev_frame = cv_cur
        return {"changed_regions": changed, "cached_regions": cached}

    # ------------------------------------------------------------------
    # Single-frame stateful API (used by bundler pipeline)
    # ------------------------------------------------------------------

    def process_frame(
        self, frame: Image.Image
    ) -> tuple[list[RegionChange], list[str]]:
        """One-frame variant — maintains internal prev. Returns (changed, cached)."""
        cv_cur = self._to_cv(frame)
        if self._prev_frame is None:
            self._prev_frame = cv_cur
            # No previous frame — treat all zones as changed (first frame)
            changed = [
                RegionChange(name=n, x=z.x, y=z.y, w=z.width, h=z.height)
                for n, z in self.zones.items()
            ]
            return changed, []
        changed, cached = self._compare(self._prev_frame, cv_cur)
        self._prev_frame = cv_cur
        return changed, cached

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_cv(img: Image.Image) -> np.ndarray:
        """Convert PIL RGB image to OpenCV BGR uint8 array."""
        arr = np.array(img.convert("RGB"), dtype=np.uint8)
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

    @staticmethod
    def _hash_crop(crop: np.ndarray) -> str:
        """MD5 of a downsampled crop — fast and collision-resistant enough."""
        small = cv2.resize(crop, (64, 64), interpolation=cv2.INTER_AREA)
        return hashlib.md5(small.tobytes()).hexdigest()

    def _compare(
        self, cv_prev: np.ndarray, cv_cur: np.ndarray
    ) -> tuple[list[RegionChange], list[str]]:
        """Core comparison logic used by both public APIs."""
        now = time.monotonic()
        changed: list[RegionChange] = []
        cached: list[str] = []

        # NOTE: zones are never evicted. An earlier 30s idle-eviction here
        # permanently destroyed zone caching after any quiet period (the
        # bundler skips this engine entirely while frame hashes are static,
        # so last_seen never updated). Found live 2026-07-15; removed.

        # If no named zones configured, do a full-frame diff
        if not self.zones:
            contours = self._contours_in_region(cv_prev, cv_cur, 0, 0, cv_cur.shape[1], cv_cur.shape[0])
            for c in contours:
                changed.append(c)
            return changed, cached

        for name, zone in self.zones.items():
            zone.last_seen = now
            h, w = cv_cur.shape[:2]
            # Clamp zone to frame dimensions
            zx = min(zone.x, w)
            zy = min(zone.y, h)
            zw = min(zone.width, w - zx)
            zh = min(zone.height, h - zy)
            if zw <= 0 or zh <= 0:
                cached.append(name)
                continue

            crop_cur = cv_cur[zy:zy+zh, zx:zx+zw]
            new_hash = self._hash_crop(crop_cur)

            # First time we see this zone — seed hash from prev frame so we
            # only flag it as changed if cur actually differs from prev.
            if not zone.last_hash:
                crop_prev_init = cv_prev[zy:zy+zh, zx:zx+zw]
                zone.last_hash = self._hash_crop(crop_prev_init)

            if new_hash == zone.last_hash:
                cached.append(name)
                zone.changed = False
                continue

            # Hash changed — run contour detection first to check significance
            contours = self._contours_in_region(cv_prev, cv_cur, zx, zy, zw, zh)
            if not contours:
                # Hash changed but all pixel changes are below min_contour_area
                # (noise/compression artifact) — treat as cached, don't update hash
                cached.append(name)
                zone.changed = False
                continue

            # Significant change confirmed — commit hash update
            zone.last_hash = new_hash
            zone.changed = True
            changed.extend(contours)

        return changed, cached

    def _contours_in_region(
        self,
        cv_prev: np.ndarray,
        cv_cur: np.ndarray,
        rx: int, ry: int, rw: int, rh: int,
    ) -> list[RegionChange]:
        """Find contours of pixel-level changes within a bounding region."""
        h, w = cv_cur.shape[:2]
        rx = max(0, rx); ry = max(0, ry)
        rw = min(rw, w - rx); rh = min(rh, h - ry)
        if rw <= 0 or rh <= 0:
            return []

        crop_prev = cv_prev[ry:ry+rh, rx:rx+rw]
        crop_cur = cv_cur[ry:ry+rh, rx:rx+rw]
        if crop_prev.shape != crop_cur.shape:
            return []

        diff = cv2.absdiff(crop_cur, crop_prev)
        gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, self.change_threshold, 255, cv2.THRESH_BINARY)

        # Dilate slightly to merge nearby pixel clusters before finding contours
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        thresh = cv2.dilate(thresh, kernel, iterations=1)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        results: list[RegionChange] = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < self.min_contour_area:
                continue
            bx, by, bw, bh = cv2.boundingRect(cnt)
            results.append(RegionChange(
                name=f"delta_{rx+bx}_{ry+by}",
                x=rx + bx, y=ry + by, w=bw, h=bh,
            ))
        return results

    def has_any_change(self) -> bool:
        return any(z.changed for z in self.zones.values())
