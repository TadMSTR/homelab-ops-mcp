# Architecture

## Package layout

```
src/homelab_ops_mcp/
├── __init__.py      Package version
├── logging.py       structlog JSON logging setup (LOG_LEVEL / LOG_FILE)
├── server.py        FastMCP app, all seven tool definitions, Host/Origin guard, main()
├── telemetry.py     Optional OTLP / InfluxDB 3 / NATS signals, off by default
└── verbclass.py     Classifies a run_command as a git/gh read or write, for logging
server.py            Root shim → homelab_ops_mcp.server:main (backward-compat)
uv.lock              The locked dependency set CI tests and a deploy installs
```

Single-purpose server — all tool logic lives in `server.py`. There is no config
module: settings come from CLI args (`--host`, `--port`, `--path`) and environment
variables, every one with a safe in-code default (see README for the full list).

## Request flow

```
MCP client → uvicorn → HostOriginGuard → FastMCP (streamable-http /mcp) → tool function
                       (421 / 403)                                          ↓
                                                         subprocess / file I/O / psutil
```

`HostOriginGuard` wraps mcp's own `TransportSecurityMiddleware` check and is handed to
`mcp.run(middleware=...)` by `main()`. FastMCP builds the transport with the SDK's check
switched off, so without it any `Host` is served. It accepts loopback names on any port.

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
during teardown. Stripping them keeps those children from aborting.
`tests/test_run_command.py` guards this behavior.

Beyond that, what a child inherits depends on `SYSTEM_OPS_CHILD_ENV_ENFORCE`. Unset
(the default) the child gets the rest of the server's environment and the allowlist is
only measured; set to `true` the child gets the allowlist alone. See README, "Child
process environment".

## Bounded work

Every tool call runs on a worker thread of one shared process, so an unbounded call
slows or kills it for every client. The bounds:

- `run_command`: per-stream capture cap, and a deadline that covers the command's exit
  as well as its output, enforced by killing the process group
- `read_file` / `read_multiple_files`: content capped at the same byte limit, the file
  streamed rather than loaded
- `edit_file`'s no-match hint: input-size caps and a one-second budget
- `verbclass`: commands parsed only up to 64 KiB

## File writes

`write_file` and `edit_file` write a temp file beside the target and rename it over,
so a crash leaves the old file or the new one. The path is first resolved one
component at a time (`_checked_resolve`), applying the `protected_symlinks` rule to
every symlink met, in a directory component or the final one. The real parent
directory is then opened through the kernel (so the kernel's own rule covers any link
swapped in since), and the temp file, the rename and `edit_file`'s read all happen
relative to that directory fd, without following links at the final name. So the
rename lands next to the real file and the link survives. A hard-linked file is refused,
because the rename would detach the name from its other links. `edit_file` decodes the
file as strict UTF-8 with no newline translation, so the bytes it did not edit are
written back unchanged, and a file that is not UTF-8 is refused.

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
