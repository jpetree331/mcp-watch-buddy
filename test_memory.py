"""Test for memory.py — runs standalone from watch_buddy/ directory."""
import sys, os, threading
sys.path.insert(0, ".")

from memory import MemoryStore

store = MemoryStore(db_path="test_watch_buddy.db")

# Test 1: session lifecycle
session_id = store.start_session("Test Stream")
assert session_id is not None, "FAIL: start_session returned None"
print(f"PASS: session started — id={session_id}")

# Test 2: log events
store.log_event("Player just got a headshot", tags=["action", "gaming"])
store.log_event("Chat is going crazy with emotes", tags=["chat"])
store.log_event("Stream title changed to night mode", tags=["ui"])
print("PASS: 3 events logged")

# Test 3: retrieve recent events
events = store.get_recent_events(n=2)
assert len(events) == 2, f"FAIL: expected 2 events, got {len(events)}"
print(f"PASS: get_recent_events returns {len(events)} events")

# Test 4: search
results = store.search_events("headshot")
assert len(results) >= 1, "FAIL: search returned no results"
assert "headshot" in results[0]["summary"].lower(), "FAIL: wrong result returned"
print(f"PASS: search found {len(results)} matching event(s)")

# Test 5: end session
store.end_session()
print("PASS: session ended cleanly")

# Test 6: concurrent write safety
errors = []
def write_events():
    try:
        for i in range(10):
            store.log_event(f"Concurrent event {i}", tags=["test"])
    except Exception as e:
        errors.append(e)

threads = [threading.Thread(target=write_events) for _ in range(3)]
[t.start() for t in threads]
[t.join() for t in threads]
assert len(errors) == 0, f"FAIL: concurrent write errors: {errors}"
print("PASS: concurrent writes handled safely")

store.close()
os.remove("test_watch_buddy.db")
print("\nAll memory tests PASSED")
