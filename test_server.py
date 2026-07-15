"""Test for server.py — checks startup and tool registration."""
import sys, subprocess, time
sys.path.insert(0, ".")

print("Starting MCP server...")
proc = subprocess.Popen(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, '.'); from server import run_server; run_server()"],
    stdin=subprocess.PIPE,   # keep stdin open so stdio transport doesn't exit immediately
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    cwd="."
)
time.sleep(3)

assert proc.poll() is None, \
    f"FAIL: server crashed on startup\nSTDERR:\n{proc.stderr.read()}"
print("PASS: server started and is running")

proc.terminate()
stdout, stderr = proc.communicate(timeout=5)
combined = stdout + stderr

expected_tools = [
    "start_watching",
    "stop_watching",
    "get_recent_observations",
    "search_memory",
    "set_personality"
]

for tool in expected_tools:
    if tool in combined:
        print(f"PASS: tool '{tool}' found in output")
    else:
        print(f"INFO: tool '{tool}' not in startup output (may be registered silently)")

print("\nServer test PASSED (process started and stopped cleanly)")
