# MCP bridge (`src/mcp/`)

A trusted [MCP](https://modelcontextprotocol.io) tool server that lets an
LLM caller (ChatGPT, HedgeHog, Trade-TF's own autonomy loop, etc.) preview
and execute orders through this engine without ever holding the admin API
key. It is **not a new capability** — every tool is a thin `httpx` call
onto the same `/v1/...` HTTP routes a human operator would use, so every
existing safety gate (approval binding, risk checks, the kill switch,
idempotency, broker/account routing) applies exactly as it does over plain
HTTP. See `src/mcp/tools.py`'s module docstring for the full rationale.

## Why this runs in its own virtualenv

The `mcp` PyPI package's pinned dependencies are incompatible with this
repo's own pinned FastAPI stack. Even the oldest `mcp==1.0.0` release
requires `pydantic>=2.8`, `anyio>=4.6`, `starlette>=0.39` — this repo pins
`fastapi==0.104.1` / `pydantic==2.5.0` / `starlette==0.27.0` /
`uvicorn==0.24.0`. Installing `mcp` into the main `.venv` silently upgrades
those packages and breaks every FastAPI route (`Router.__init__()` starts
rejecting a keyword argument fastapi 0.104.1 still passes it) — this was
verified directly, not assumed.

`requirements-mcp.txt` also pins `mcp<2` — `mcp` 2.x renamed
`mcp.server.fastmcp.FastMCP` to `mcp.server.mcpserver.MCPServer` and
changed other APIs; `src/mcp/server.py` is written against the v1
`FastMCP` API, matching Trade-TF's own `trader_tf/mcp/server.py`.

So `mcp` is **not** part of `pyproject.toml`'s main dependency list.
Instead:

- `src/mcp/tools.py` never imports the `mcp` package. `register(mcp)`
  takes the `FastMCP` instance as a plain `Any` and only calls its
  `.tool()` decorator — so this module (and its tests, in
  `tests/test_mcp_tools.py`) run fine in the main venv/test suite, with a
  minimal fake standing in for `FastMCP`.
- `src/mcp/server.py` is the one file that actually does
  `from mcp.server.fastmcp import FastMCP`, and it only ever runs from a
  **separate** virtualenv built from `requirements-mcp.txt`.

## Running it

```bash
python -m venv .venv-mcp
.venv-mcp/bin/pip install -r requirements-mcp.txt   # or .venv-mcp\Scripts\pip on Windows
.venv-mcp/bin/python -m src.mcp.server
```

The main Execution-Engine API server must already be running separately
(`uvicorn src.api.server:app`) — the bridge is a client of it, not a
replacement for it.

## Configuration

Two `Settings` fields (`src/config.py`), read from the main app's own
`.env`/environment (the bridge process needs `API_KEY_ADMIN` set to the
real admin key so it can inject it into every request itself):

- `MCP_TRANSPORT` (default `"stdio"`) — matches Trade-TF's own
  `trader_tf_mcp_transport` setting/default, for one coherent story across
  both repos.
- `MCP_TARGET_BASE_URL` (default `"http://localhost:8000"`) — where the
  real Execution-Engine API is listening.

## Tools

| Tool | HTTP route | Notes |
|---|---|---|
| `health` | `GET /v1/health` | DB + broker connectivity check. |
| `preview_order` | `POST /v1/orders/preview` | Validates + risk-checks a proposal; never submits. |
| `execute_order` | `POST /v1/orders/execute` | Requires a genuine approval artifact; the engine still enforces preview-binding, expiry, and the kill switch. |
| `get_order_status` | `GET /v1/orders/{decision_id}` | Read-only. |
| `cancel_order` | `POST /v1/orders/{decision_id}/cancel` | Requests broker cancellation; never invents a terminal state. |
| `get_positions` | `GET /v1/account/{account}/positions` | Read-only, broker-reported. |
| `reconcile` | `POST /v1/reconciliation/positions` | Can only halt trading (kill switch) on mismatch, never resume it. |

Deliberately absent, on purpose, same as Trade-TF's own MCP surface:
anything that could place a live order without going through
`execute_order`'s own approval gate — there is no `submit_order`,
`force_execute`, or broker-credential tool here.

The admin key never appears in any tool's parameters — it's injected by
`src/mcp/tools.py`'s `_client()` from `settings.api_key_admin`, server-side,
on every request. An MCP caller can do exactly what an authenticated HTTP
caller could, no more.
