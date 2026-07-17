# AGENTS.md — homelab-ops-mcp

MCP server that gives AI agents shell access, file read/write, and process inspection on a local host. Built with FastMCP, deployed via PM2 (venv `python server.py`), served over streamable HTTP.

## What it does

Exposes six tools:

- **`run_command`** — executes arbitrary shell commands via `bash -c`, returns stdout/stderr/exit code
- **`read_file`** — reads a file with optional line range
- **`write_file`** — overwrites a file (creates parent dirs by default)
- **`edit_file`** — find-and-replace edit; `old_str` must match exactly once
- **`read_directory`** — lists directory contents, optionally recursive
- **`list_processes`** — lists running processes sorted by CPU, memory, or PID

## Structure

```
src/homelab_ops_mcp/
├── __init__.py   # Package version
├── logging.py    # structlog JSON logging (LOG_LEVEL / LOG_FILE)
└── server.py     # FastMCP app, all tool definitions, main() entry point
server.py         # Root shim → homelab_ops_mcp.server:main (backward-compat)
tests/            # pytest suite (~96% coverage)
```

All settings come from CLI args (`--host`, `--port`, `--path`) or the logging env
vars — no config file. See `ARCHITECTURE.md` for detail.

## Running locally

```bash
pip install -e ".[dev]"
homelab-ops-mcp --host 0.0.0.0 --port 8282 --path /mcp
# or the backward-compatible shim:
python server.py --host 0.0.0.0 --port 8282 --path /mcp
```

## Development

```bash
ruff check . && ruff format --check .
pytest --cov=homelab_ops_mcp --cov-report=term-missing
pip-audit --strict .
```

CI (`.github/workflows/ci.yml`) runs these across Python 3.11–3.13.

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
