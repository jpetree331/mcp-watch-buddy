"""Watch Buddy — MCP server entrypoint.

Run directly:
    python main.py

Or via Claude Desktop claude_desktop_config.json:
    {
      "mcpServers": {
        "watch-buddy": {
          "command": "python",
          "args": ["C:/absolute/path/to/watch_buddy/main.py"]
        }
      }
    }

NO API key required. Claude Desktop uses its own subscription for inference.
"""

try:
    from .server import run_server
except ImportError:
    from server import run_server  # type: ignore[no-reattr]


if __name__ == "__main__":
    run_server()
