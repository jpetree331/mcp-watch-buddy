"""Ears (v1.2) — pipeline tests with a mock transcriber (no audio device,
no network, no API key needed)."""
import threading
import time

import numpy as np

from ears import EarStream


def _mono_bytes(loud: bool, seconds: float = 1.0, rate: int = 16000) -> bytes:
    n = int(seconds * rate)
    if loud:
        t = np.arange(n) / rate
        wave_ = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
    else:
        wave_ = np.zeros(n, dtype=np.int16)
    return wave_.tobytes()


def _run_worker(ear: EarStream, chunks: list):
    """Feed chunks through the real worker loop, then stop it."""
    now = time.time()
    for i, c in enumerate(chunks):
        ear._chunk_q.append((now - 2 + i, now - 1 + i, c, 16000))
    ear._stop_event.clear()
    t = threading.Thread(target=ear._worker_loop, daemon=True)
    t.start()
    ear._chunk_ready.set()
    deadline = time.time() + 5
    while time.time() < deadline and ear._chunk_q:
        time.sleep(0.05)
    time.sleep(0.2)
    ear._stop_event.set()
    ear._chunk_ready.set()
    t.join(timeout=3)


def test_silence_gate():
    calls = []
    ear = EarStream({"enabled": True}, transcribe_fn=lambda wav: calls.append(wav) or "should not happen")
    _run_worker(ear, [_mono_bytes(loud=False)])
    assert ear.chunks_silent == 1 and ear.chunks_transcribed == 0
    assert not calls, "silent chunk must never reach the transcriber"
    print("PASS: silent chunks are gated locally — zero API calls")


def test_speech_chunk_transcribed_and_recallable():
    ear = EarStream({"enabled": True, "log_to_file": False},
                    transcribe_fn=lambda wav: "it's-a me, Mario")
    _run_worker(ear, [_mono_bytes(loud=True)])
    assert ear.chunks_transcribed == 1
    recent = ear.get_recent(30)
    assert "Mario" in recent["text"]
    assert recent["lines"] and recent["lines"][0].startswith("[")
    print("PASS: loud chunk transcribed, timestamped, recallable via get_recent")


def test_window_excludes_old_entries():
    ear = EarStream({"enabled": True, "log_to_file": False},
                    transcribe_fn=lambda wav: "x")
    old = time.time() - 300
    with ear._lock:
        ear._entries.append({"t0": old, "t1": old + 10, "text": "ancient words"})
        ear._entries.append({"t0": time.time() - 5, "t1": time.time(), "text": "fresh words"})
    recent = ear.get_recent(30)
    assert "fresh words" in recent["text"] and "ancient words" not in recent["text"]
    print("PASS: get_recent windows correctly")


def test_cursor_exactly_once():
    """Jess's cursor: each delivery covers exactly the audio since the last
    one — no duplicates, no gaps."""
    ear = EarStream({"enabled": True, "log_to_file": False},
                    transcribe_fn=lambda wav: "x")
    now = time.time()
    with ear._lock:
        ear._entries.append({"t0": now - 20, "t1": now - 15, "text": "first words"})
        ear._entries.append({"t0": now - 10, "t1": now - 5, "text": "second words"})
    # First delivery from a cursor before both entries: gets both
    d1 = ear.get_since(now - 30)
    assert "first words" in d1["text"] and "second words" in d1["text"]
    # Cursor advanced to "now": nothing new
    d2 = ear.get_since(now)
    assert d2["text"] == "" and d2["lines"] == []
    # New entry arrives after the cursor: delivered exactly once
    with ear._lock:
        ear._entries.append({"t0": now + 1, "t1": now + 2, "text": "third words"})
    d3 = ear.get_since(now)
    assert d3["text"] == "third words"
    assert "first words" not in d3["text"], "duplicates must not re-deliver"
    print("PASS: delivery cursor — no duplicates, no gaps, exactly once")


def test_config_disable():
    ear = EarStream({"enabled": False}).start()
    assert ear.status == "disabled (config)"
    print("PASS: config toggle disables cleanly")


if __name__ == "__main__":
    test_silence_gate()
    test_speech_chunk_transcribed_and_recallable()
    test_window_excludes_old_entries()
    test_cursor_exactly_once()
    test_config_disable()
    print("\nAll ears v5 tests passed")
