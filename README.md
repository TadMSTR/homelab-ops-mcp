# homelab-ops-mcp

A [FastMCP](https://github.com/jlowin/fastmcp) server that provides shell command execution, file system access, and process inspection over the [MCP Streamable-HTTP transport](https://modelcontextprotocol.io/specification/). Built for homelab operations agents that need to read files, run commands, and inspect processes on the host machine.

## Security Warning

This server provides **unrestricted shell access** to the host machine. Any MCP client with a connection can run arbitrary commands, read any file the process user can access, and write to any writable path. Deploy it only behind a trusted network boundary and on services you control. It is not designed for multi-user or internet-exposed environments.

## Tools

| Tool | Description |
|------|-------------|
| `run_command` | Execute a shell command (`bash -c`), returns stdout/stderr/exit_code |
| `read_file` | Read a file by absolute path, optional `start_line`/`end_line` |
| `write_file` | Write/overwrite a file, creates parent dirs by default |
| `edit_file` | Find-and-replace edit — `old_str` must match exactly once |
| `read_directory` | List directory contents, optional recursive with `max_depth` |
| `list_processes` | List running processes sorted by cpu/mem/pid, optional name filter |

## Transport

Streamable-HTTP on `http://<host>:<port>/mcp` (default `127.0.0.1:8282`).

The bind host defaults to `127.0.0.1` (loopback only). Because this server offers
**unauthenticated** shell execution and filesystem access, network isolation is the only
control — keep it loopback-bound unless you have a specific reason to expose it.

If an MCP client runs in its own container or network namespace and must reach the server
across it, bind `--host 0.0.0.0` explicitly — loopback isn't reachable from another
namespace. Do this only on a trusted, isolated network, and be aware that a container
reaching the host via `host.docker.internal` bypasses any SSRF guard that blocks private
IP ranges.

## Installation

```bash
pip install .           # or: pip install -e .   for development
```

This installs the `homelab_ops_mcp` package and a `homelab-ops-mcp` console script.

## Running

```bash
homelab-ops-mcp                          # default host 127.0.0.1 (loopback), port 8282
homelab-ops-mcp --port 9090              # custom port
homelab-ops-mcp --host 0.0.0.0           # expose to containers/LAN (unauthenticated — use with care)
```

A backward-compatible `python server.py --host … --port …` shim is retained for
existing deployments (it just calls the package entry point after an editable install).

With PM2:
```bash
pm2 start homelab-ops-mcp --name homelab-ops-mcp -- --port 8282
pm2 save
```

### Logging

Structured JSON logs go to stderr by default. Tune via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `LOG_FILE` | _(unset)_ | Append logs to this path instead of stderr |

Command text and file contents are logged at `DEBUG` only; `INFO` records carry
non-sensitive metadata (paths, cwd, exit codes).

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest --cov=homelab_ops_mcp --cov-report=term-missing
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow and [ARCHITECTURE.md](ARCHITECTURE.md)
for the layout. CI runs the same checks across Python 3.11–3.13.

## MCP Client Configuration

### LibreChat (`librechat.yaml`)

```yaml
mcpServers:
  homelab-ops:
    type: streamable-http
    url: http://127.0.0.1:8282/mcp
```

If LibreChat runs in a container (so the server is on a different host/namespace), use
`http://host.docker.internal:8282/mcp` and start the server with `--host 0.0.0.0` — see the
Transport section for the security caveats.

### Claude Code (`.claude/claude_desktop_config.json` or MCP settings)

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

### Claude Desktop (`claude_desktop_config.json`)

Claude Desktop can connect via streamable-http:

```json
{
  "mcpServers": {
    "homelab-ops": {
      "url": "http://localhost:8282/mcp"
    }
  }
}
```

## Default Working Directory

`run_command` defaults the working directory to the current user's home directory (`Path.home()`). Override per-call with the `cwd` parameter.

## License

MIT
