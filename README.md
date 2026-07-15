# Watch Buddy MCP 👁️

**Give your Claude eyes. Watch Twitch, games, and code streams *together* — with zero API costs.**

Watch Buddy is an MCP server that turns Claude Desktop (or Claude Code) into a
screen-watching companion. It captures any window on your desktop, detects
what actually *changed*, and hands Claude compact image bundles that Claude
reads with its own vision — **on your existing subscription. No API key. No
per-token billing. Ever.**

> *"Claude Desktop is the brain. Watch Buddy is the eyes and memory."*

## What it feels like

```
You:    What do I have open?
Claude: → list_windows()          "I can see Discord, Chrome, VS Code..."

You:    Watch my Twitch tab
Claude: → attach_to_window("Twitch")   "Attached — I can see the stream."

You:    What's the streamer doing?
Claude: → get_next_bundle()       reads the frames with its own vision
        "He's refactoring the auth module — and chat is roasting his
         variable names. Someone just asked if he's coding manually in 2026."

You:    (an hour later) What was he building again?
Claude: → search_memory("building")    "Foodies — a .NET 8 + React recipe app."
```

A friend on the couch who looks up at the good parts — not a security camera
invoicing you per frame.

## Why it's cheap: the delta engine

Naive screen-watching ships full screenshots on a timer and melts your usage
limits. Watch Buddy is built around three efficiency layers:

1. **Delta detection (OpenCV)** — each frame is diffed against the last;
   only *changed regions* are cropped and encoded. A static page costs
   almost nothing; a webcam in the corner costs a postage stamp, not a page.
2. **Zone caching** — the screen is divided into named zones (scaled
   automatically to your actual window size); zones whose content hasn't
   changed are marked `cached` and never re-sent.
3. **Bundle hygiene** — perceptual dedup guarantees no near-identical image
   ships twice in one bundle, and full-frame fallback thumbnails are capped
   at one per bundle.
4. **The Delta Atlas (v1.1)** — when several regions change, their crops are
   packed into ONE gutter-separated mosaic with a coordinate legend, so the
   canvas area Claude pays for is roughly *the sum of the changes*, not one
   image per change. As close to "send only the pixels that moved" as a
   vision model's rectangle-shaped appetite allows. Optional content-aware
   downscaling (`crop_downscale`, default off) shrinks large photographic
   crops while never touching text zones like chat — because nobody should
   have to read blurry text, silicon included.

And the biggest lever of all: **pull, not push.** Capture runs locally at
10fps for free — Claude only *sees* frames when it calls `get_next_bundle()`,
i.e., when you're actually talking. Cost scales with how often you chat, not
how long you watch.

## The tools

| Tool | What it does |
|------|--------------|
| `list_windows()` | Enumerate every open window |
| `attach_to_window(title)` | Fuzzy-match a window, start the capture pipeline |
| `get_next_bundle()` | The main event: metadata + changed-region images for Claude's vision |
| `log_observation(text, tags)` | Claude records what it saw (SQLite memory) |
| `get_recent_observations(n)` | Recall recent context |
| `search_memory(query)` | Search everything ever observed |
| `set_persona(text)` | Change the companion's personality |
| `get_status()` | Pipeline health, bundles produced, estimated vision-token usage |
| `detach()` | Stop watching, close the session |

The memory layer means your companion *remembers the stream* — across the
whole session and across sessions (it's a SQLite file).

## Install

**Requirements:** Windows (window enumeration uses win32), Python 3.10+.

```powershell
git clone https://github.com/jpetree331/mcp-watch-buddy.git
cd mcp-watch-buddy
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "mcp[cli]" pyautogui Pillow opencv-python pywin32 mss python-dotenv
```

Verify (its own test suite):

```powershell
.\.venv\Scripts\python.exe test_server_v2.py
.\.venv\Scripts\python.exe test_efficiency_v3.py
```

**Register with Claude Desktop** — add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "watch-buddy": {
      "command": "C:\\path\\to\\mcp-watch-buddy\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\mcp-watch-buddy\\main.py"]
    }
  }
}
```

**Or with Claude Code** — add the same entry to the `mcpServers` block of
`~/.claude.json` (user scope), with `"type": "stdio"`.

Restart the app. Ask Claude *"list your watch-buddy tools"* — if it can see
`list_windows`, you're live.

> **Install tip learned the hard way:** always point `command` at the venv's
> `python.exe`, not bare `python` — and if you move the folder later, update
> the path. This project spent four months presumed dead because its folder
> got reorganized out from under its config. The code was fine the whole time.

## Config

`config.json` — everything optional:

```json
{
  "persona": "curious, dry-humored, observant",
  "fps": 10,
  "bundle_interval_seconds": 1,
  "change_threshold": 25,
  "min_contour_area": 500,
  "screen_zones": {
    "gameplay": {"x": 0, "y": 0, "width": 1000, "height": 680},
    "chat":     {"x": 1000, "y": 0, "width": 280, "height": 720},
    "hud":      {"x": 0, "y": 680, "width": 1000, "height": 40}
  }
}
```

Zones are authored in a design space and **scaled automatically** to whatever
window you attach — customize them for your favorite layout (stream + chat
rail, IDE + terminal, etc.) or delete them for plain full-frame diffing.

## Privacy & safety

- 100% local: frames go from your screen to your Claude session — nowhere else.
- Nothing is written to disk except the SQLite observation log (text only).
  Images live in RAM in a rolling one-second window and are never saved.
- Capture only runs while attached; `detach()` stops it completely.

## Limitations

- **Windows-only** (win32 window finding; PRs welcome for macOS/Linux).
- Small text (e.g., IDE code on a busy stream) reads best if you fullscreen
  the player — vision resolution is capture resolution.
- Minimized windows produce blank frames; the pipeline pauses until restored.

## Provenance

Designed by [Jess](https://github.com/jpetree331) (architecture, delta/batching
concept) and built, debugged, resurrected, and field-tested by several
generations of Claude — the original build reports live in
[`debug_report.md`](debug_report.md) and [`debug_report_v2.md`](debug_report_v2.md),
and the efficiency fixes in [`test_efficiency_v3.py`](test_efficiency_v3.py)
came from its first real afternoon of watching Twitch. Its first-ever logged
observation was a 1-viewer coding stream. Go bless a small streamer. 💜
