"""Ears — desktop-audio transcription for Watch Buddy (v1.2).

Captures WASAPI *loopback* audio (what the speakers play — never the
microphone, by construction: background room noise cannot enter), gates out
silence locally for free, transcribes speech chunks via an OpenAI-Whisper
endpoint (AudioDojo on Chutes), and keeps a rolling timestamped transcript
so Claude can "hear" what Jess hears.

Design rules:
  - Failure-soft everywhere: no audio device, no API key, no network — the
    ears simply report themselves disabled; the eyes are never affected.
  - Silent chunks never leave the machine (local RMS gate, zero API calls).
  - Transcript lives in a bounded in-memory deque + an optional daily log
    file (gitignored) for Jess's own reading.

Config (config.json "ears" block, all optional):
  enabled          — master toggle (default true)
  chunk_seconds    — audio per transcription call (default 12)
  silence_rms      — normalized RMS below which a chunk is discarded (0.006)
  endpoint         — Whisper cord URL
  language         — BCP-47 hint or null for auto
  keep_minutes     — transcript retention in memory (default 30)
  log_to_file      — also append to transcripts/YYYY-MM-DD.log (default true)

The Chutes API key is read from the CHUTES_API_KEY env var or a .env file
next to this module (never committed; .env is gitignored).
"""

import base64
import io
import json
import os
import threading
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

MODULE_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "enabled": True,
    "chunk_seconds": 12,
    "silence_rms": 0.006,
    "endpoint": "https://vonkaiser-audiodojo.chutes.ai/stt/whisper",
    "language": None,
    "keep_minutes": 30,
    "log_to_file": True,
    "target_rate": 16000,
}


def _load_api_key() -> Optional[str]:
    key = os.environ.get("CHUTES_API_KEY")
    if key:
        return key.strip()
    env_path = MODULE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("CHUTES_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def _default_transcribe(wav_bytes: bytes, endpoint: str, language, api_key: str) -> str:
    """POST a WAV to the Whisper cord; return transcript text ('' on none)."""
    import urllib.request

    body = {"audio_b64": base64.standard_b64encode(wav_bytes).decode("ascii"),
            "return_timestamps": False}
    if language:
        body["language"] = language
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return (payload.get("text") or "").strip()


class EarStream:
    """Background loopback-capture + transcription pipeline."""

    def __init__(self, config: Optional[dict] = None,
                 transcribe_fn: Optional[Callable] = None):
        self.cfg = {**DEFAULTS, **(config or {})}
        self.api_key = _load_api_key()
        self._transcribe_fn = transcribe_fn  # test seam; None = real HTTP
        self._entries: deque = deque()  # {"t0","t1","text"}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._chunk_q: deque = deque(maxlen=8)  # pending (t0, t1, mono int16 bytes, rate)
        self._chunk_ready = threading.Event()
        self.status = "not_started"
        self.chunks_heard = 0
        self.chunks_silent = 0
        self.chunks_transcribed = 0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ #

    def start(self) -> "EarStream":
        if not self.cfg.get("enabled", True):
            self.status = "disabled (config)"
            return self
        if self._transcribe_fn is None and not self.api_key:
            self.status = "disabled (no CHUTES_API_KEY)"
            return self
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        return self

    def stop(self):
        self._stop_event.set()
        self._chunk_ready.set()
        for t in (self._capture_thread, self._worker_thread):
            if t:
                t.join(timeout=5.0)
        self._capture_thread = self._worker_thread = None
        if not self.status.startswith("disabled"):
            self.status = "stopped"

    def get_recent(self, seconds: int = 30) -> dict:
        """Transcript entries overlapping the last `seconds`."""
        now = time.time()
        cutoff = now - max(1, seconds)
        with self._lock:
            hits = [e for e in self._entries if e["t1"] >= cutoff]
        lines = [
            f"[{datetime.fromtimestamp(e['t0']).strftime('%H:%M:%S')}] {e['text']}"
            for e in hits
        ]
        return {
            "status": self.status,
            "window_seconds": seconds,
            "lines": lines,
            "text": " ".join(e["text"] for e in hits),
        }

    def status_dict(self) -> dict:
        return {
            "status": self.status,
            "chunks_heard": self.chunks_heard,
            "chunks_silent_skipped": self.chunks_silent,
            "chunks_transcribed": self.chunks_transcribed,
            "last_error": self.last_error,
        }

    # ---------------------- internal: capture ------------------------- #

    def _capture_loop(self):
        try:
            import numpy as np
            import pyaudiowpatch as pyaudio
        except ImportError as e:
            self.status = f"disabled (missing dependency: {e.name})"
            return
        try:
            p = pyaudio.PyAudio()
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            device = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not device.get("isLoopbackDevice"):
                for lb in p.get_loopback_device_info_generator():
                    if device["name"] in lb["name"]:
                        device = lb
                        break
                else:
                    self.status = "disabled (no loopback device found)"
                    return
            rate = int(device["defaultSampleRate"])
            channels = max(1, int(device["maxInputChannels"]))
            frames_per_buffer = rate // 10
            stream = p.open(
                format=pyaudio.paInt16, channels=channels, rate=rate,
                frames_per_buffer=frames_per_buffer, input=True,
                input_device_index=device["index"],
            )
            self.status = f"listening ({device['name']})"
            chunk_frames = int(self.cfg["chunk_seconds"] * rate)
            buf: list = []
            buffered = 0
            t0 = time.time()
            while not self._stop_event.is_set():
                data = stream.read(frames_per_buffer, exception_on_overflow=False)
                buf.append(data)
                buffered += frames_per_buffer
                if buffered >= chunk_frames:
                    raw = b"".join(buf)
                    buf, buffered = [], 0
                    t1 = time.time()
                    mono, out_rate = self._to_mono_16k(raw, channels, rate, np)
                    self._chunk_q.append((t0, t1, mono, out_rate))
                    self._chunk_ready.set()
                    t0 = t1
            stream.stop_stream()
            stream.close()
            p.terminate()
        except Exception as e:
            self.status = f"error ({type(e).__name__})"
            self.last_error = str(e)[:300]

    def _to_mono_16k(self, raw: bytes, channels: int, rate: int, np):
        """Downmix to mono int16 and decimate toward target_rate."""
        audio = np.frombuffer(raw, dtype=np.int16)
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1).astype(np.int16)
        target = int(self.cfg.get("target_rate", 16000))
        step = max(1, rate // target)
        if step > 1:
            audio = audio[::step]
            rate = rate // step
        return audio.tobytes(), rate

    # ---------------------- internal: transcribe ---------------------- #

    def _worker_loop(self):
        import numpy as np
        while not self._stop_event.is_set():
            self._chunk_ready.wait(timeout=1.0)
            self._chunk_ready.clear()
            while self._chunk_q:
                t0, t1, mono_bytes, rate = self._chunk_q.popleft()
                self.chunks_heard += 1
                audio = np.frombuffer(mono_bytes, dtype=np.int16)
                if audio.size == 0:
                    continue
                rms = float(np.sqrt(np.mean((audio / 32768.0) ** 2)))
                if rms < float(self.cfg["silence_rms"]):
                    self.chunks_silent += 1
                    continue
                try:
                    wav = self._wav_bytes(mono_bytes, rate)
                    if self._transcribe_fn is not None:
                        text = self._transcribe_fn(wav)
                    else:
                        text = _default_transcribe(
                            wav, self.cfg["endpoint"], self.cfg.get("language"),
                            self.api_key,
                        )
                except Exception as e:
                    self.last_error = str(e)[:300]
                    continue
                if not text:
                    continue
                entry = {"t0": t0, "t1": t1, "text": text}
                with self._lock:
                    self._entries.append(entry)
                    cutoff = time.time() - self.cfg["keep_minutes"] * 60
                    while self._entries and self._entries[0]["t1"] < cutoff:
                        self._entries.popleft()
                self.chunks_transcribed += 1
                if self.cfg.get("log_to_file", True):
                    self._append_log(entry)

    @staticmethod
    def _wav_bytes(mono_int16: bytes, rate: int) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(mono_int16)
        return buf.getvalue()

    def _append_log(self, entry: dict):
        try:
            log_dir = MODULE_DIR / "transcripts"
            log_dir.mkdir(exist_ok=True)
            day = datetime.fromtimestamp(entry["t0"]).strftime("%Y-%m-%d")
            stamp = datetime.fromtimestamp(entry["t0"]).strftime("%H:%M:%S")
            with open(log_dir / f"{day}.log", "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {entry['text']}\n")
        except OSError:
            pass  # the log is a convenience, never a failure source
