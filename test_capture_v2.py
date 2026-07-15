"""test_capture.py v2 — includes window attachment test"""
import sys, time; sys.path.insert(0, ".")
from capture import Capture

cap = Capture()
frames = []
start = time.time()
while time.time() - start < 2:
    frame = cap.get_frame()
    if frame:
        frames.append(frame)
    time.sleep(0.1)

assert len(frames) >= 12, f"FAIL: only {len(frames)} frames in 2s"
assert frames[0].size == (cap.region["width"], cap.region["height"]), \
    f"FAIL: frame size {frames[0].size} != region {(cap.region['width'], cap.region['height'])}"
print(f"PASS: {len(frames)} frames captured, size {frames[0].size}")

# Window attachment test
from window_finder import list_windows, get_window_region
windows = list_windows()
if windows:
    region = get_window_region(windows[0], fuzzy=False)
    if region:
        cap2 = Capture(region=region)
        frame = cap2.get_frame()
        assert frame is not None, "FAIL: window-attached capture failed"
        assert frame.size == (region["width"], region["height"]), \
            f"FAIL: window frame size {frame.size} != {(region['width'], region['height'])}"
        print(f"PASS: window-attached capture works — {frame.size}")
        cap2.close()

cap.close()
print("\nAll capture v2 tests PASSED")
