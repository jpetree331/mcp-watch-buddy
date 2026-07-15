"""Integration test v2 — uses WatchBuddyServer class directly"""
import sys, time; sys.path.insert(0, ".")

from server import WatchBuddyServer
server = WatchBuddyServer()

# Test 1: list_windows
windows = server.list_windows()
assert isinstance(windows, list) and len(windows) > 0, \
    "FAIL: list_windows returned empty"
print(f"PASS: list_windows found {len(windows)} windows")

# Test 2: attach to first available window
first_window = windows[0]
result = server.attach_to_window(first_window)
assert result["status"] == "ok", f"FAIL: attach failed: {result}"
print(f"PASS: attached to '{result['window']}'")

# Test 3: pipeline runs for 3 seconds
time.sleep(3)
status = server.get_status()
assert status["running"] == True, "FAIL: pipeline not running"
print(f"PASS: pipeline running — {status['bundles_produced']} bundles produced")

# Test 4: get_next_bundle returns correct schema
bundle = server.get_next_bundle()
assert "frames" in bundle, "FAIL: bundle missing frames"
assert "metadata" in bundle, "FAIL: bundle missing metadata"
assert "timestamp" in bundle, "FAIL: bundle missing timestamp"
assert "has_changes" in bundle, "FAIL: bundle missing has_changes"
assert "window_title" in bundle, "FAIL: bundle missing window_title"
print(f"PASS: bundle schema correct — has_changes={bundle['has_changes']}, "
      f"frames={len(bundle['frames'])}")

# Test 5: log_observation round trip
obs_result = server.log_observation(
    "Test observation from integration test",
    tags=["test"]
)
assert obs_result["status"] == "ok", f"FAIL: log_observation failed: {obs_result}"
recent = server.get_recent_observations(n=1)
assert len(recent) == 1, f"FAIL: observation not retrievable, got {len(recent)}"
assert "Test observation" in recent[0]["summary"], \
    f"FAIL: wrong observation: {recent[0]}"
print("PASS: observation logged and retrieved correctly")

# Test 6: detach cleans up
result = server.detach()
assert result["status"] == "ok", f"FAIL: detach failed: {result}"
time.sleep(1)
status = server.get_status()
assert status["running"] == False, "FAIL: pipeline still running after detach"
print("PASS: detach stops pipeline cleanly")

print("\nAll integration tests passed — ready for Claude Desktop")
