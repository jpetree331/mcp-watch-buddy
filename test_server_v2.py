"""test_server.py v2 — checks startup, tools, no anthropic imports"""
import sys, subprocess, time, os; sys.path.insert(0, ".")

print("Starting MCP server...")
proc = subprocess.Popen(
    [sys.executable, "server.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    stdin=subprocess.PIPE,
    text=True,
    cwd="."
)
time.sleep(3)

assert proc.poll() is None, \
    f"FAIL: server crashed\nSTDERR:\n{proc.stderr.read()}"
print("PASS: server started and running")

proc.terminate()
stdout, stderr = proc.communicate(timeout=5)

# Verify tools via direct import (FastMCP logs nothing to stderr)
import asyncio
import warnings; warnings.filterwarnings("ignore")
from server import mcp
tools = asyncio.run(mcp.list_tools())
tool_names = {t.name for t in tools}

required_tools = [
    "list_windows", "attach_to_window", "detach", "get_next_bundle",
    "log_observation", "get_recent_observations", "search_memory",
    "set_persona", "get_status",
]
missing = [t for t in required_tools if t not in tool_names]
for t in required_tools:
    if t in tool_names:
        print(f"PASS: tool '{t}' registered")
    else:
        print(f"FAIL: tool '{t}' NOT registered")

assert not missing, f"Missing tools: {missing}"

# Verify NO anthropic imports anywhere
for fname in ["server.py", "main.py", "capture.py", "delta.py",
              "bundler.py", "memory.py", "window_finder.py"]:
    if not os.path.exists(fname):
        continue
    content = open(fname, encoding="utf-8").read()
    assert "import anthropic" not in content, \
        f"FAIL: {fname} imports anthropic — wrong architecture"
    assert "ANTHROPIC_API_KEY" not in content, \
        f"FAIL: {fname} references API key — wrong architecture"
    print(f"PASS: {fname} — no API imports")

print("\nAll server v2 tests passed")
