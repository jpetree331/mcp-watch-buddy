"""Watch Buddy MCP server — pure data pipeline, zero API calls.

Claude Desktop IS the brain. This server is the eyes and memory.

Tool flow:
  1. Claude calls list_windows()            → find what to watch
  2. Claude calls attach_to_window(title)   → start capture pipeline
  3. Claude calls get_next_bundle()         → receive frames as base64 PNG
  4. Claude processes images with its OWN vision (subscription — no API billing)
  5. Claude calls log_observation(text)     → store what it noticed
  6. Claude calls get_recent_observations() → recall recent context
"""

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP

try:
    from .bundler import FrameBundler
    from .capture import ScreenCapture
    from .delta import DeltaEngine
    from .memory import MemoryStore
    from .window_finder import get_window_region
    from .window_finder import list_windows as _list_windows_impl
except ImportError:
    from bundler import FrameBundler           # type: ignore[no-reattr]
    from capture import ScreenCapture          # type: ignore[no-reattr]
    from delta import DeltaEngine              # type: ignore[no-reattr]
    from memory import MemoryStore             # type: ignore[no-reattr]
    from window_finder import get_window_region  # type: ignore[no-reattr]
    from window_finder import list_windows as _list_windows_impl  # type: ignore[no-reattr]

mcp = FastMCP("watch-buddy")

CONFIG_PATH = Path(__file__).parent / "config.json"

# Maximum total base64 payload size per bundle (bytes) before downsampling
_MAX_BUNDLE_BYTES = 4 * 1024 * 1024  # 4 MB


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {
        "persona": "curious, dry-humored, observant — you notice things others miss",
        "fps": 10,
        "bundle_interval_seconds": 1,
        "change_threshold": 25,
        "min_contour_area": 500,
        "screen_zones": {},
    }


# ------------------------------------------------------------------
# Pipeline state (module-level singleton, protected by a lock)
# ------------------------------------------------------------------

class _PipelineState:
    def __init__(self):
        self.lock = threading.Lock()
        self.capture: Optional[ScreenCapture] = None
        self.bundler: Optional[FrameBundler] = None
        self.memory: Optional[MemoryStore] = None
        self.latest_bundle: Optional[dict] = None
        self.latest_bundle_time: float = 0.0
        self.window_title: Optional[str] = None
        self.region: Optional[dict] = None
        self.bundles_produced: int = 0
        self.observations_logged: int = 0
        self.persona: str = _load_config().get("persona", "curious, dry-humored, observant")
        self._capture_alive: bool = False  # set by capture thread health check
        # Usage tracking (reset on each attach_to_window)
        self.images_delivered: int = 0       # total images sent to Claude this session
        self.image_bytes_sent: int = 0       # raw base64 bytes of image data
        self.estimated_vision_tokens: int = 0  # ~pixels/750, Claude's approximate formula

    def is_running(self) -> bool:
        return self.capture is not None

    def pipeline_healthy(self) -> bool:
        """True if capture is running and has produced a frame recently (<5s)."""
        if not self.is_running():
            return False
        return time.monotonic() - self._last_frame_time < 5.0

    # Tracks time of last frame received from capture loop
    _last_frame_time: float = 0.0


_state = _PipelineState()


def _on_bundle_ready(bundle: dict):
    """Bundler callback — store latest bundle, enforce size cap."""
    # Enforce 4MB total payload limit
    total = sum(len(f) for f in bundle.get("frames", []))
    if total > _MAX_BUNDLE_BYTES:
        import base64, io
        from PIL import Image
        shrunk = []
        for b64 in bundle["frames"]:
            raw = base64.b64decode(b64)
            img = Image.open(io.BytesIO(raw))
            img = img.resize((img.width // 2, img.height // 2), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            shrunk.append(base64.standard_b64encode(buf.getvalue()).decode("ascii"))
        bundle = dict(bundle, frames=shrunk)

    with _state.lock:
        existing = _state.latest_bundle
        # Don't overwrite an unconsumed bundle that has frames (e.g. the baseline)
        # with an empty one. Carry forward the frames so Claude always gets at
        # least the most recent non-empty snapshot when it polls.
        if existing and existing.get("frames") and not bundle.get("frames"):
            bundle = dict(bundle, frames=existing["frames"])
        _state.latest_bundle = bundle
        _state.latest_bundle_time = time.monotonic()
        _state.bundles_produced += 1


def _on_frame(frame, ts: float):
    """Capture callback — update health timestamp, forward to bundler."""
    _state._last_frame_time = time.monotonic()
    if _state.bundler:
        _state.bundler.ingest_frame(frame, ts)


# ------------------------------------------------------------------
# MCP Tools
# ------------------------------------------------------------------

@mcp.tool()
async def list_windows() -> list:
    """List all currently open windows on the desktop.

    Returns a sorted list of window title strings.
    Use this to discover what application or game to watch.
    """
    return _list_windows_impl()


@mcp.tool()
async def attach_to_window(window_title: str) -> dict:
    """Attach the capture pipeline to a window and start frame capture.

    Fuzzy-matches window_title against open windows (case-insensitive
    substring match). Stops any previously running capture first.

    Args:
        window_title: Partial or full title of the window to watch
    """
    # Stop any existing pipeline first
    _stop_pipeline()

    region_info = get_window_region(window_title)
    if not region_info:
        return {
            "status": "error",
            "message": (
                f"No visible window found matching '{window_title}'. "
                "Call list_windows() to see available windows."
            ),
        }

    # Copy to avoid mutating the returned dict
    region_info = dict(region_info)
    matched_title = region_info.pop("window_title")
    region = region_info  # {x, y, width, height}

    cfg = _load_config()
    with _state.lock:
        _state.persona = cfg.get("persona", _state.persona)

    delta = DeltaEngine(
        screen_zones=cfg.get("screen_zones", {}),
        change_threshold=cfg.get("change_threshold", 25),
        min_contour_area=cfg.get("min_contour_area", 500),
        frame_size=(region.get("width", 0), region.get("height", 0)),
    )
    bundler = FrameBundler(
        delta_engine=delta,
        bundle_interval=cfg.get("bundle_interval_seconds", 1),
        region=region,
        window_title=matched_title,
    )
    bundler.on_bundle_ready = _on_bundle_ready

    capture = ScreenCapture(region, fps=cfg.get("fps", 10))
    memory = MemoryStore()
    memory.start_session(matched_title)

    # v1.2 — Ears: desktop-audio transcription (WASAPI loopback, never the
    # mic). Toggle lives in config ("ears".enabled, default true).
    # Failure-soft: any problem leaves ears disabled and eyes unaffected.
    ear_stream = None
    try:
        from ears import EarStream
        ear_stream = EarStream(cfg.get("ears", {})).start()
    except Exception:
        ear_stream = None

    with _state.lock:
        _state.capture = capture
        _state.bundler = bundler
        _state.ears = ear_stream
        _state.memory = memory
        _state.window_title = matched_title
        _state.region = region
        _state.latest_bundle = None
        _state.latest_bundle_time = 0.0
        _state.bundles_produced = 0
        _state.observations_logged = 0
        _state.images_delivered = 0
        _state.image_bytes_sent = 0
        _state.estimated_vision_tokens = 0
        _state._last_frame_time = time.monotonic()

    bundler.start()
    capture.start(_on_frame)

    return {
        "status": "ok",
        "window": matched_title,
        "region": region,
    }


def _stop_pipeline():
    """Internal helper — tear down capture/bundler/memory without async."""
    with _state.lock:
        capture = _state.capture
        bundler = _state.bundler
        memory = _state.memory
        ear_stream = getattr(_state, "ears", None)
        obs_count = _state.observations_logged
        _state.capture = None
        _state.bundler = None
        _state.memory = None
        _state.ears = None
        _state.window_title = None
        _state.region = None
        _state.latest_bundle = None

    if capture:
        capture.stop()
    if bundler:
        bundler.stop()
    if ear_stream:
        ear_stream.stop()
    if memory:
        memory.end_session()
    return obs_count


@mcp.tool()
async def detach() -> dict:
    """Stop capture and close the current watch session.

    Returns the number of observations logged this session.
    """
    obs_count = _stop_pipeline()
    return {"status": "ok", "observations_logged": obs_count}


@mcp.tool()
async def get_next_bundle() -> list:
    """Return the most recently completed frame bundle.

    This is the PRIMARY tool. Call this to receive the current screen
    as vision-ready image content. Claude Desktop processes frames using
    its own vision capability (subscription — no API billing).

    Returns a content list:
      - First item: TextContent with JSON metadata:
          {"status", "window_title", "frame_count", "has_changes",
           "timestamp", "bundle_age_seconds", "metadata"}
      - Subsequent items: ImageContent (PNG) — one per changed screen zone

    Returns status "not_running" if no window is attached.
    Returns status "waiting" with no images if no new bundle is ready yet.
    """
    with _state.lock:
        if not _state.is_running():
            return [mcp_types.TextContent(
                type="text",
                text=json.dumps({"status": "not_running"}),
            )]
        bundle = _state.latest_bundle
        bundle_time = _state.latest_bundle_time
        window_title = _state.window_title or ""
        _state.latest_bundle = None  # consume it

    if bundle is None:
        waiting = {
            "status": "waiting",
            "has_changes": False,
            "frame_count": 0,
            "window_title": window_title,
            "bundle_age_seconds": 0.0,
        }
        ear_stream = getattr(_state, "ears", None)
        if ear_stream is not None:
            waiting["transcript_30s"] = ear_stream.get_recent(30)
        return [mcp_types.TextContent(type="text", text=json.dumps(waiting))]

    age = round(time.monotonic() - bundle_time, 2)

    # Metadata without frames — send as text so it doesn't consume vision tokens
    meta = {k: v for k, v in bundle.items() if k != "frames"}
    meta["status"] = "ok"
    meta["bundle_age_seconds"] = age

    # v1.2 Ears — the last 30 seconds of desktop audio, transcribed, rides
    # along with every bundle so sight and sound describe the same moment.
    ear_stream = getattr(_state, "ears", None)
    if ear_stream is not None:
        meta["transcript_30s"] = ear_stream.get_recent(30)

    # Measure image data and estimate vision tokens before returning
    import base64 as _b64, io as _io
    from PIL import Image as _Image
    total_bytes = 0
    total_tokens = 0
    image_items = []
    for frame_b64 in bundle.get("frames", []):
        raw = _b64.b64decode(frame_b64)
        total_bytes += len(raw)
        try:
            img = _Image.open(_io.BytesIO(raw))
            total_tokens += (img.width * img.height) // 750
        except Exception:
            total_tokens += len(raw) // 750  # fallback: bytes as rough proxy
        image_items.append(mcp_types.ImageContent(
            type="image",
            data=frame_b64,
            mimeType="image/png",
        ))

    with _state.lock:
        _state.images_delivered += len(image_items)
        _state.image_bytes_sent += total_bytes
        _state.estimated_vision_tokens += total_tokens

    content: list = [mcp_types.TextContent(type="text", text=json.dumps(meta))]
    content.extend(image_items)
    return content


@mcp.tool()
async def log_observation(text: str, tags: list = []) -> dict:
    """Store an observation made after processing a bundle.

    Call this AFTER processing a get_next_bundle() response.
    This closes the perception loop and builds session memory.

    If no session is active, a default one is auto-started so observations
    can always be logged regardless of pipeline state.

    Args:
        text: What you observed (be specific — this is your memory)
        tags: Optional labels e.g. ["action", "chat", "ui", "event"]
    """
    with _state.lock:
        memory = _state.memory

    if memory is None:
        # Auto-start a session so observations can always be stored
        memory = MemoryStore()
        memory.start_session("standalone-observations")
        with _state.lock:
            _state.memory = memory

    memory.log_event(text, tags=list(tags) if tags else None)
    with _state.lock:
        _state.observations_logged += 1
        event_id = _state.observations_logged

    return {"status": "ok", "event_id": event_id}


@mcp.tool()
async def get_recent_observations(n: int = 5) -> list:
    """Return the n most recent logged observations from this session.

    Use this before processing a new bundle to recall context about
    what you have already seen.

    Args:
        n: Number of observations to return (default 5)
    """
    with _state.lock:
        memory = _state.memory

    if memory is None:
        return []
    return memory.get_recent_events(n)


@mcp.tool()
async def search_memory(query: str) -> list:
    """Search all logged observations for a keyword or phrase.

    Args:
        query: Text to search for in observation summaries
    """
    with _state.lock:
        memory = _state.memory

    if memory is None:
        return []
    return memory.search_events(query)


@mcp.tool()
async def set_persona(persona: str) -> dict:
    """Update the companion's personality description stored in config.json.

    Args:
        persona: New personality description
                 e.g. 'laconic speedrun commentator who only speaks when something notable happens'
    """
    cfg = _load_config()
    cfg["persona"] = persona
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    with _state.lock:
        _state.persona = persona
    return {"status": "ok", "persona": persona}


@mcp.tool()
async def get_transcript(seconds: int = 30) -> dict:
    """Return the desktop-audio transcript from the last N seconds.

    Watch Buddy's EARS (v1.2): WASAPI loopback audio — what the speakers
    play, NEVER the microphone — silence-gated locally, transcribed via
    Whisper (AudioDojo on Chutes). Pair with get_next_bundle to see AND
    hear the same moment.

    Returns {status, window_seconds, lines (timestamped), text (joined)}.
    Ears start automatically with attach_to_window when config
    "ears".enabled is true (the default).
    """
    ear_stream = getattr(_state, "ears", None)
    if ear_stream is None:
        return {"status": "ears not running — attach to a window first "
                          "(or ears are disabled in config)"}
    return ear_stream.get_recent(max(1, int(seconds)))


@mcp.tool()
async def set_ears(enabled: bool) -> dict:
    """Toggle the ears at runtime and persist the choice to config.json.

    enabled=False stops any live transcription immediately; enabled=True
    starts listening now if a watch session is active (and applies to all
    future sessions either way).
    """
    cfg = _load_config()
    ears_cfg = cfg.get("ears", {})
    ears_cfg["enabled"] = bool(enabled)
    cfg["ears"] = ears_cfg
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

    ear_stream = getattr(_state, "ears", None)
    if not enabled and ear_stream is not None:
        ear_stream.stop()
        with _state.lock:
            _state.ears = None
        return {"status": "ok", "ears": "stopped and disabled"}
    if enabled and ear_stream is None and _state.is_running():
        try:
            from ears import EarStream
            with _state.lock:
                _state.ears = EarStream(ears_cfg).start()
            return {"status": "ok", "ears": _state.ears.status}
        except Exception as e:
            return {"status": "ok", "ears": f"enabled but failed to start: {e}"}
    return {"status": "ok", "ears": "enabled" if enabled else "disabled"}


@mcp.tool()
async def get_status() -> dict:
    """Return current pipeline state and health.

    Returns:
      running          — whether capture is active
      window_title     — attached window name (or null)
      region           — capture region dict (or null)
      bundles_produced — total bundles produced this session
      observations_logged — total observations stored this session
      persona          — current companion personality
      pipeline_healthy — True if a frame was received in the last 5s
    """
    with _state.lock:
        mb_sent = round(_state.image_bytes_sent / 1_048_576, 2)
        return {
            "running": _state.is_running(),
            "window_title": _state.window_title,
            "region": _state.region,
            "bundles_produced": _state.bundles_produced,
            "observations_logged": _state.observations_logged,
            "persona": _state.persona,
            "pipeline_healthy": _state.pipeline_healthy(),
            "usage": {
                "images_delivered": _state.images_delivered,
                "image_data_mb": mb_sent,
                "estimated_vision_tokens": _state.estimated_vision_tokens,
                "note": "token estimate uses ~1 token per 750 pixels (approximate)",
            },
            "ears": (getattr(_state, "ears", None).status_dict()
                     if getattr(_state, "ears", None) else {"status": "not running"}),
        }


# ------------------------------------------------------------------
# WatchBuddyServer — synchronous wrapper for integration tests and
# programmatic use without running the MCP event loop.
# ------------------------------------------------------------------

class WatchBuddyServer:
    """Synchronous facade over the MCP tool functions.

    Used by integration tests and callers that don't run the MCP stdio loop.
    All methods mirror the MCP tool signatures exactly.
    """

    def list_windows(self) -> list:
        return _list_windows_impl()

    def attach_to_window(self, window_title: str) -> dict:
        import asyncio
        return asyncio.run(_attach(window_title))

    def detach(self) -> dict:
        obs_count = _stop_pipeline()
        return {"status": "ok", "observations_logged": obs_count}

    def get_next_bundle(self) -> dict:
        """Returns the bundle as a schema-complete dict.

        content[0] is the metadata TextContent; any ImageContent items are
        reassembled under "frames" so non-MCP callers (tests, scripts) get a
        stable schema on BOTH the success and waiting paths (2026-07-15 fix).
        """
        import asyncio
        content = asyncio.run(_get_bundle())
        result = json.loads(content[0].text)
        result["frames"] = [c.data for c in content[1:]]
        result.setdefault("metadata", [])
        result.setdefault("timestamp", "")
        result.setdefault("has_changes", False)
        result.setdefault("window_title", "")
        return result

    def log_observation(self, text: str, tags: list = []) -> dict:
        import asyncio
        return asyncio.run(_log_obs(text, tags))

    def get_recent_observations(self, n: int = 5) -> list:
        with _state.lock:
            memory = _state.memory
        if memory is None:
            return []
        return memory.get_recent_events(n)

    def search_memory(self, query: str) -> list:
        with _state.lock:
            memory = _state.memory
        if memory is None:
            return []
        return memory.search_events(query)

    def get_status(self) -> dict:
        with _state.lock:
            mb_sent = round(_state.image_bytes_sent / 1_048_576, 2)
            return {
                "running": _state.is_running(),
                "window_title": _state.window_title,
                "region": _state.region,
                "bundles_produced": _state.bundles_produced,
                "observations_logged": _state.observations_logged,
                "persona": _state.persona,
                "pipeline_healthy": _state.pipeline_healthy(),
                "usage": {
                    "images_delivered": _state.images_delivered,
                    "image_data_mb": mb_sent,
                    "estimated_vision_tokens": _state.estimated_vision_tokens,
                },
            }

    def set_persona(self, persona: str) -> dict:
        import asyncio
        return asyncio.run(_set_persona(persona))


# Private async helpers for WatchBuddyServer (avoid name collision with MCP tools)
async def _attach(window_title: str) -> dict:
    return await attach_to_window(window_title)

async def _get_bundle() -> dict:
    return await get_next_bundle()

async def _log_obs(text: str, tags: list) -> dict:
    return await log_observation(text, tags)

async def _set_persona(persona: str) -> dict:
    return await set_persona(persona)


def run_server():
    """Start the MCP server over stdio (for Claude Desktop)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_server()
