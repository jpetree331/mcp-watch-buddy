"""Screen capture module — grabs a configurable screen region at target FPS.

Exposes two interfaces:
  Capture  — simple poll-based: cap.get_frame() returns one PIL Image
  ScreenCapture — callback-based background thread for the bundler pipeline
"""

import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import pyautogui
from PIL import Image

# Optional fast backend via mss (lower overhead than pyautogui on Windows)
try:
    import mss
    _MSS_AVAILABLE = True
except ImportError:
    _MSS_AVAILABLE = False


def _load_region() -> dict:
    cfg_path = Path(__file__).parent / "config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        # "region" key is optional; fall back to full-screen default
        return cfg.get("region", {"x": 0, "y": 0, "width": 1280, "height": 720})
    return {"x": 0, "y": 0, "width": 1280, "height": 720}


def _clamp_region(region: dict) -> dict:
    """Clamp region to the monitor that contains it.

    Uses mss to enumerate all monitors (supports multi-monitor setups).
    Falls back to pyautogui primary screen if mss is unavailable.
    """
    rx, ry, rw, rh = region["x"], region["y"], region["width"], region["height"]

    if _MSS_AVAILABLE:
        try:
            with mss.mss() as sct:
                # Find the monitor whose rect contains the window's top-left corner
                for mon in sct.monitors[1:]:  # index 0 is the virtual combined screen
                    mx, my = mon["left"], mon["top"]
                    mw, mh = mon["width"], mon["height"]
                    if mx <= rx < mx + mw and my <= ry < my + mh:
                        # Clamp to this monitor
                        x = max(mx, rx)
                        y = max(my, ry)
                        w = min(rw, mx + mw - x)
                        h = min(rh, my + mh - y)
                        return {"x": x, "y": y, "width": max(1, w), "height": max(1, h)}
                # Window not clearly inside any monitor — use combined virtual screen
                mon = sct.monitors[0]
                x = max(mon["left"], rx)
                y = max(mon["top"], ry)
                w = min(rw, mon["left"] + mon["width"] - x)
                h = min(rh, mon["top"] + mon["height"] - y)
                return {"x": x, "y": y, "width": max(1, w), "height": max(1, h)}
        except Exception:
            pass

    # Fallback: primary monitor only
    sw, sh = pyautogui.size()
    x = max(0, min(rx, sw - 1))
    y = max(0, min(ry, sh - 1))
    w = min(rw, sw - x)
    h = min(rh, sh - y)
    return {"x": x, "y": y, "width": max(1, w), "height": max(1, h)}


class Capture:
    """Simple synchronous frame grabber. Call get_frame() to pull one frame."""

    def __init__(self, region: Optional[dict] = None):
        raw = region or _load_region()
        self.region = _clamp_region(raw)
        # Prefer mss on Windows — faster and doesn't require display focus
        self._use_mss = _MSS_AVAILABLE
        if self._use_mss:
            self._sct = mss.mss()

    def get_frame(self) -> Optional[Image.Image]:
        """Capture and return one PIL Image frame, or None on error.

        Returns None (with a warning) if the frame is entirely black —
        which typically means the source window is minimized.
        """
        try:
            img = self._grab_mss() if self._use_mss else self._grab_pyautogui()
        except Exception as e:
            print(f"[capture] grab error (retrying pyautogui): {e}")
            try:
                img = self._grab_pyautogui()
            except Exception as e2:
                print(f"[capture] fallback also failed: {e2}")
                return None

        # Detect blank/black frame — window is likely minimized
        if img is not None and self._is_blank(img):
            print("[capture] warning: blank frame detected — window may be minimized")
            return None
        return img

    @staticmethod
    def _is_blank(img: Image.Image) -> bool:
        """Return True if the image is entirely or nearly entirely black."""
        import numpy as np
        arr = np.array(img)
        return arr.mean() < 2.0  # mean pixel value < 2/255 = essentially black

    def _grab_mss(self) -> Image.Image:
        r = self.region
        monitor = {"top": r["y"], "left": r["x"], "width": r["width"], "height": r["height"]}
        shot = self._sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return img

    def _grab_pyautogui(self) -> Image.Image:
        r = self.region
        img = pyautogui.screenshot(region=(r["x"], r["y"], r["width"], r["height"]))
        if img is None:
            raise RuntimeError("pyautogui.screenshot returned None")
        return img.convert("RGB")

    def close(self):
        if self._use_mss:
            self._sct.close()


class ScreenCapture:
    """Callback-based background capture loop for the bundler pipeline."""

    def __init__(self, region: dict, fps: int = 10):
        self._capture = Capture(region)
        self.fps = fps
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self, on_frame: Callable[[Image.Image, float], None]):
        """Start capturing frames on a daemon thread.

        on_frame(image, timestamp) is called for each captured frame.
        """
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, args=(on_frame,), daemon=True
        )
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._capture.close()

    def _loop(self, on_frame: Callable[[Image.Image, float], None]):
        interval = 1.0 / self.fps
        while self._running:
            t0 = time.perf_counter()
            frame = self._capture.get_frame()
            if frame is not None:
                try:
                    on_frame(frame, time.time())
                except Exception as e:
                    print(f"[capture] on_frame callback error: {e}")
            elapsed = time.perf_counter() - t0
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)
