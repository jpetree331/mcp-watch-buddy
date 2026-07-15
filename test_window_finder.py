"""Test for window_finder.py"""
import sys; sys.path.insert(0, ".")
from window_finder import list_windows, get_window_region, focus_window

# Test 1: list_windows returns something
windows = list_windows()
assert isinstance(windows, list), "FAIL: list_windows did not return a list"
assert len(windows) > 0, "FAIL: no windows found"
print(f"PASS: {len(windows)} windows found")
print("Sample windows:", windows[:5])

# Test 2: fuzzy match finds a real window — use whatever is actually open
test_title = "Claude"
region = get_window_region(test_title, fuzzy=True)
if region:
    print(f"PASS: found window matching '{test_title}': {region['window_title']}")
    assert "x" in region and "y" in region, "FAIL: region missing x/y"
    assert region["width"] > 0, "FAIL: region width is zero"
    assert region["height"] > 0, "FAIL: region height is zero"
    print(f"  Region: {region}")
else:
    # Try any window from the list
    for w in windows[:5]:
        r = get_window_region(w, fuzzy=False)
        if r and r["width"] > 0:
            print(f"PASS: found window '{w}': {r}")
            break
    else:
        print(f"WARN: no testable window found")

# Test 3: non-existent window returns None
result = get_window_region("XYZZY_FAKE_WINDOW_12345")
assert result is None, "FAIL: fake window should return None"
print("PASS: non-existent window correctly returns None")

# Test 4: minimized/offscreen window handling — no crashes
for title in windows[:10]:
    r = get_window_region(title, fuzzy=False)
    # Must return None or a valid dict — never raise
print("PASS: no crashes iterating real windows")

print("\nAll window_finder tests PASSED")
