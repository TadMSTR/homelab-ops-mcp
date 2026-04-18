---
tier: baseline
---

# AGENTS.md — homelab-ops-mcp

MCP server that gives AI agents shell access, file read/write, and process inspection on a local host. Built with FastMCP, deployed in Docker, served over streamable HTTP.

## What it does

Exposes five tools:

- **`run_command`** — executes arbitrary shell commands via `bash -c`, returns stdout/stderr/exit code
- **`read_file`** — reads a file with optional line range
- **`write_file`** — overwrites a file (creates parent dirs by default)
- **`edit_file`** — find-and-replace edit; `old_str` must match exactly once
- **`read_directory`** — lists directory contents, optionally recursive
- **`list_processes`** — lists running processes sorted by CPU, memory, or PID

## Structure

```
server.py    # All server logic — FastMCP app, all tool definitions
```

Single-file server. No routes directory, no config file — all settings via CLI args or environment.

## Running locally

```bash
pip install fastmcp psutil
python server.py --host 0.0.0.0 --port 8282 --path /mcp
```

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
