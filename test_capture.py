"""Test for capture.py — runs standalone from watch_buddy/ directory."""
import sys, time
sys.path.insert(0, ".")

from capture import Capture

cap = Capture()
frames = []
start = time.time()
while time.time() - start < 2:
    frame = cap.get_frame()
    if frame:
        frames.append(frame)
    time.sleep(0.1)

print(f"Captured {len(frames)} frames in 2 seconds (expect ~20 at 10fps)")
print(f"Frame size: {frames[0].size if frames else 'NO FRAMES'}")
print(f"Frame mode: {frames[0].mode if frames else 'NO FRAMES'}")

assert len(frames) >= 12, f"FAIL: only {len(frames)} frames captured (need >= 12 for ~6fps minimum)"
print("PASS: frame count OK")
import json
cfg = json.load(open("config.json"))
region = cfg.get("region", {"width": 1280, "height": 720})
expected_size = (region["width"], region["height"])
assert frames[0].size == expected_size, f"FAIL: size {frames[0].size} != {expected_size}"
print("PASS: frame size matches config")
assert frames[0].mode in ("RGB", "RGBA"), f"FAIL: mode={frames[0].mode}"
print("PASS: frame mode is RGB/RGBA")
print("\nAll capture tests PASSED")
