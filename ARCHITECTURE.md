# Architecture

## Package layout

```
src/homelab_ops_mcp/
├── __init__.py      Package version
├── logging.py       structlog JSON logging setup (LOG_LEVEL / LOG_FILE)
└── server.py        FastMCP app + all six tool definitions + main() entry point
server.py            Root shim → homelab_ops_mcp.server:main (backward-compat)
```

Single-purpose server — all tool logic lives in `server.py`. There is no config
module: settings come from CLI args (`--host`, `--port`, `--path`) and two logging
environment variables.

## Request flow

```
MCP client → FastMCP (streamable-http /mcp) → tool function
                                                   ↓
                                          subprocess / pathlib / psutil
```

Each tool is a plain function decorated with `@mcp.tool`. Under FastMCP 3.x the
decorator returns the original callable, so the tools are unit-tested by importing
and calling them directly.

## Environment sanitisation (HLOPS-1)

`run_command` builds the child environment via `_clean_env()`, which copies the
process environment minus PM2's IPC variables:

- `NODE_CHANNEL_FD`
- `NODE_CHANNEL_SERIALIZATION_MODE`
- `NODE_UNIQUE_ID`

When the server runs under PM2, these leak into spawned children; any Node.js
child (node, pnpm, tsc, …) then inherits a stray IPC file descriptor and SIGABRTs
during teardown. Stripping them keeps shelled-out commands on a clean environment.
`tests/test_run_command.py` guards this behavior.

## Logging

`logging.configure_logging()` sets up structlog for JSON output at import time.
`LOG_LEVEL` (default `INFO`) sets the filtering level; `LOG_FILE` redirects output
from stderr to a file (falling back to stderr if the path can't be opened).
Command text and file contents are logged at `DEBUG` only — `INFO` records carry
non-sensitive metadata (paths, cwd, exit codes) suitable for an audit trail.

## Security posture

`run_command` executes arbitrary shell commands as the process owner, and the file
tools read/write anywhere the process user can reach. This is intentional for a
trusted single-tenant ops agent behind a network boundary — see
[SECURITY.md](SECURITY.md). The server is not designed for multi-user or
internet-exposed deployment.
