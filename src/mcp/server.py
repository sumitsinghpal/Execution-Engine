"""
Execution-Engine MCP server entrypoint.

Run from a SEPARATE virtualenv (see docs/MCP_BRIDGE.md and requirements-
mcp.txt) — the `mcp` package's pinned dependencies are incompatible with
this repo's own pinned FastAPI stack, and src/mcp/tools.py is written so
it never needs the real `mcp` package to be tested (see that module's
docstring). This file is the one place that actually imports it.

    python -m src.mcp.server
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.mcp import tools

mcp = FastMCP("execution-engine")

tools.register(mcp)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
