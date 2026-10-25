# AGENTS.md — homelab-ops-mcp

MCP server that gives AI agents shell access, file read/write, and process inspection on a local host. Built with FastMCP, deployed via PM2 (venv `python server.py`), served over streamable HTTP.

## What it does

Exposes seven tools:

- **`run_command`** — executes arbitrary shell commands via `bash -c`, returns stdout/stderr/exit code, `execution_time`, and a `truncated` flag
- **`read_file`** — reads a file with optional line range
- **`read_multiple_files`** — reads several files in one call; a failing path does not fail the rest
- **`write_file`** — overwrites a file (creates parent dirs by default)
- **`edit_file`** — find-and-replace edit; `old_str` must match exactly once. Supports `dry_run`, and reports the closest passage with a diff when nothing matches
- **`read_directory`** — lists directory contents, optionally recursive
- **`list_processes`** — lists running processes sorted by CPU, memory, or PID

## Structure

```
src/homelab_ops_mcp/
├── __init__.py   # Package version
├── logging.py    # structlog JSON logging (LOG_LEVEL / LOG_FILE)
├── server.py     # FastMCP app, all tool definitions, Host/Origin guard, main()
├── telemetry.py  # Optional OTLP / InfluxDB 3 / NATS signals, off by default
└── verbclass.py  # git/gh read-or-write classification for run_command logs
server.py         # Root shim → homelab_ops_mcp.server:main (backward-compat)
tests/            # pytest suite (~98% coverage)
uv.lock           # Locked dependency set
```

All settings come from CLI args (`--host`, `--port`, `--path`) or environment
variables — no config file. The README documents every variable: logging
(`LOG_LEVEL`, `LOG_FILE`), the child environment (`SYSTEM_OPS_CHILD_ENV_ENFORCE`,
`SYSTEM_OPS_CHILD_ENV_ALLOWLIST`), output and read size (`SYSTEM_OPS_OUTPUT_LIMIT_BYTES`),
extra accepted `Host` values (`SYSTEM_OPS_ALLOWED_HOSTS`) and telemetry. Every one has a
safe in-code default. See `ARCHITECTURE.md` for detail.

`write_file` and `edit_file` write through symlinks and refuse hard-linked files;
`edit_file` changes only the matched bytes and refuses non-UTF-8 files. Requests whose
`Host` is not a loopback name are refused with 421.

Paths passed to the file tools and to `run_command`'s `cwd` must be absolute. They are
rejected otherwise — `Path()` does no tilde expansion, so a `~/...` path would resolve
against the process working directory instead.

## Running locally

```bash
uv sync --extra dev
uv run homelab-ops-mcp --port 8282 --path /mcp      # binds 127.0.0.1
# or the backward-compatible shim:
uv run python server.py --port 8282 --path /mcp
```

Keep the default loopback bind. `--host 0.0.0.0` exposes unauthenticated shell access
to the network; see README, "Transport".

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest --cov=homelab_ops_mcp --cov-report=term-missing
```

CI (`.github/workflows/ci.yml`) runs these across Python 3.11–3.13 from `uv.lock`,
with and without the `[telemetry]` extra and against the newest allowed versions, and
audits the locked set. `secret-scan.yml` runs gitleaks. Change a dependency in
`pyproject.toml` and commit the `uv lock` result with it.

## Wiring into a Claude config (streamable HTTP)

```json
{
  "mcpServers": {
    "homelab-ops": {
      "type": "http",
      "url": "http://localhost:8282/mcp"
    }
  }
}
```

## Security considerations

`run_command` executes arbitrary shell commands as the process owner — scope network access carefully. This server is designed for internal/trusted use only. Do not expose it to the public internet.

## Git workflow

Branch before editing — do not commit directly to `main`.
