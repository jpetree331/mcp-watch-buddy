"""Test for delta.py — runs standalone from watch_buddy/ directory."""
import sys
sys.path.insert(0, ".")

from PIL import Image, ImageDraw
from delta import DeltaEngine

engine = DeltaEngine()

# Test 1: identical frames -> no deltas
frame_a = Image.new("RGB", (1280, 720), color=(50, 50, 50))
frame_b = Image.new("RGB", (1280, 720), color=(50, 50, 50))
result = engine.process(frame_a, frame_b)
assert len(result["changed_regions"]) == 0, f"FAIL: identical frames produced changes: {result}"
print("PASS: identical frames produce no delta")

# Test 2: large changed region detected
frame_c = frame_a.copy()
draw = ImageDraw.Draw(frame_c)
draw.rectangle([200, 200, 600, 500], fill=(255, 100, 0))
result = engine.process(frame_a, frame_c)
assert len(result["changed_regions"]) > 0, "FAIL: large change not detected"
print(f"PASS: large change detected — {len(result['changed_regions'])} region(s)")

# Test 3: tiny 5px change below threshold ignored
frame_d = frame_a.copy()
draw = ImageDraw.Draw(frame_d)
draw.rectangle([200, 200, 203, 203], fill=(255, 0, 0))
result = engine.process(frame_a, frame_d)
assert len(result["changed_regions"]) == 0, f"FAIL: noise not filtered — got {result['changed_regions']}"
print("PASS: tiny change correctly filtered as noise")

# Test 4: caching — same content twice -> cached on second call
result1 = engine.process(frame_a, frame_c)
result2 = engine.process(frame_c, frame_c)
assert len(result2["cached_regions"]) > 0, "FAIL: caching not working"
print(f"PASS: caching works — {result2['cached_regions']} marked cached")

# Test 5: two disconnected large regions detected separately
frame_e = frame_a.copy()
draw = ImageDraw.Draw(frame_e)
draw.rectangle([100, 100, 300, 200], fill=(0, 255, 0))
draw.rectangle([900, 500, 1100, 650], fill=(0, 0, 255))
result = engine.process(frame_a, frame_e)
assert len(result["changed_regions"]) >= 2, \
    f"FAIL: expected 2+ regions, got {len(result['changed_regions'])}"
print(f"PASS: multiple regions detected — {len(result['changed_regions'])} found")

print("\nAll delta tests PASSED")
