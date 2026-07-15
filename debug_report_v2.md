# Watch Buddy v2 — Debug Report
**Date:** 2026-03-28
**Architecture:** Subscription-based MCP server — zero API calls, zero API key
**Status:** All components PASS

---

## Architecture confirmation

Zero instances of `import anthropic` or `ANTHROPIC_API_KEY` in any source file.
Verified by automated check in `test_server_v2.py` across all 7 source modules.

Claude Desktop is the brain. Watch Buddy is the eyes and memory.

---

## Bug index

| # | Component | Bug | Severity | Fix |
|---|-----------|-----|----------|-----|
| WF-1 | window_finder.py | `list_windows` function named `list_open_windows` — test import failed | Critical | Renamed to `list_windows`; added `list_open_windows = list_windows` alias for internal callers |
| WF-2 | window_finder.py | Multiple matches on fuzzy title returned arbitrary window, not foreground | Medium | Collect all candidates, sort by `is_foreground` flag, first-match wins |
| WF-3 | window_finder.py | UWP/Microsoft Store apps not enumerated by win32gui | Low | Added PowerShell `Get-Process | Where MainWindowTitle` fallback; deduplicates against win32gui results |
| CAP-1 | capture.py | `_clamp_region` used `pyautogui.size()` (primary monitor only) — windows on secondary monitor clamped to zero width | High | Rewrote to enumerate all monitors via mss; clamps to the monitor containing the window's top-left corner |
| CAP-2 | capture.py | No blank/minimized frame detection — sent all-black frames downstream | Medium | Added `_is_blank()` check (mean pixel < 2/255); returns `None` with warning for blank frames |
| SRV-1 | server.py | `list_windows` tool called `list_open_windows()` — stale name | Critical | Updated to call `_list_windows_impl()` (the renamed window_finder function) |
| SRV-2 | server.py | `region_info.pop("window_title")` mutated the dict returned by `get_window_region` | Medium | Copy the dict before pop: `region_info = dict(region_info)` |
| SRV-3 | server.py | `log_observation` failed with "No active session" when called without `attach_to_window` | Medium | Auto-starts a `standalone-observations` MemoryStore session if none exists |
| SRV-4 | server.py | `get_next_bundle` returned stale `_state.window_title` after releasing lock | Low | Capture `window_title` inside the lock before returning no-bundle response |
| SRV-5 | server.py | No `bundle_age_seconds` field in bundle output | Low | Added `bundle_age_seconds = time.monotonic() - bundle_time` to returned bundle dict |
| SRV-6 | server.py | No `pipeline_healthy` in `get_status` | Low | Added `_last_frame_time` tracking; `pipeline_healthy = (now - last_frame_time) < 5s` |
| SRV-7 | server.py | No bundle size cap — large regions could exceed MCP message limits | Medium | Added `_MAX_BUNDLE_BYTES = 4MB` check; downsamples all frames to 50% if exceeded |
| SRV-8 | server.py | No `WatchBuddyServer` class — integration test couldn't import it | Critical | Added `WatchBuddyServer` synchronous facade over all MCP tool functions |
| SRV-9 | server.py | `get_next_bundle` returned `{}` on no-bundle-yet — test expected `{"status": "waiting"}` | Low | Returns `{"status": "waiting", "has_changes": false, ...}` when bundle not yet produced |
| SRV-10 | server.py | `list_windows` MCP tool name collided with `window_finder.list_windows` import | Critical | Imported window_finder function as `_list_windows_impl` to avoid decorator name shadowing |
| SRV-11 | server.py | `detach()` was async but called synchronously in `_stop_pipeline` helper | Medium | Extracted `_stop_pipeline()` sync helper; `detach` MCP tool delegates to it |
| SRV-12 | server.py | `attach_to_window` called `await detach()` which re-acquired lock inside locked section | Medium | Call `_stop_pipeline()` directly (no lock nesting) |
| TST-1 | test_server_v2.py | `open(fname).read()` used default cp1252 encoding — failed on UTF-8 source files | Medium | Fixed to `open(fname, encoding="utf-8")` |

---

## Component test results

| Component | Test file | Result | Notes |
|-----------|-----------|--------|-------|
| window_finder.py | test_window_finder.py | **PASS** | 16 windows found, fuzzy match, None for fake window, no crash on minimized |
| capture.py | test_capture.py | **PASS** | 14 frames/2s, correct size |
| capture.py | test_capture_v2.py | **PASS** | Window attachment, multi-monitor support verified |
| delta.py | test_delta.py | **PASS** | All 5 assertions unchanged |
| bundler.py | test_bundler.py | **PASS** | 3 bundles, PNG encoding, `window_title`/`frame_count`/`has_changes` fields present |
| memory.py | test_memory.py | **PASS** | All 6 assertions unchanged |
| server.py | test_server_v2.py | **PASS** | 9 tools registered, zero API imports confirmed |
| Integration | test_integration_v2.py | **PASS** | list→attach→bundle→log→recall→detach all pass |

---

## Zero API verification

Files scanned: `server.py`, `main.py`, `capture.py`, `delta.py`, `bundler.py`, `memory.py`, `window_finder.py`

- `import anthropic`: **NOT FOUND** in any file
- `ANTHROPIC_API_KEY`: **NOT FOUND** in any file
- `claude_client.py`: **DOES NOT EXIST**
- `requirements.txt` anthropic entry: **ABSENT**

---

## requirements.txt (final)

```
mcp[cli]>=1.0.0
pyautogui>=0.9.54
Pillow>=10.0.0
opencv-python>=4.8.0
pywin32>=306
mss>=9.0.0
python-dotenv>=1.0.0
```

No `anthropic` entry. No API key needed. Ever.

---

## Claude Desktop installation

### 1. Install dependencies
```
pip install mcp[cli] pyautogui Pillow opencv-python pywin32 mss python-dotenv
```

### 2. Add to `claude_desktop_config.json`

Location: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "watch-buddy": {
      "command": "python",
      "args": ["C:\\absolute\\path\\to\\watch_buddy\\main.py"]
    }
  }
}
```

Replace `C:\\absolute\\path\\to\\watch_buddy\\main.py` with the actual path.

### 3. Restart Claude Desktop

### 4. Verify installation
In Claude Desktop: *"List your available tools"*

You should see: `list_windows`, `attach_to_window`, `detach`, `get_next_bundle`,
`log_observation`, `get_recent_observations`, `search_memory`, `set_persona`, `get_status`

---

## Usage flow

```
You: "What do I have open?"
Claude: calls list_windows() → "I can see Discord, Firefox, VS Code..."

You: "Watch my Discord"
Claude: calls attach_to_window("Discord") → "Attached — watching Discord"

You: "What's happening?"
Claude: calls get_next_bundle() → receives base64 PNG frames
        processes images WITH ITS OWN VISION (subscription)
        calls log_observation("Server activity in #general, someone posted an image")
        responds conversationally

You: "What did you see earlier?"
Claude: calls get_recent_observations(5) → returns stored summaries
```

---

## Known limitations

- **Windows only** — `window_finder.py` uses win32gui/pywin32
- **Static screen** — if attached window has no visual changes, bundles are empty (correct behavior)
- **Minimized windows** — blank frame detection returns None; pipeline pauses silently until window is restored
- **Multi-monitor** — capture works across monitors; mss handles negative/extended coordinates
