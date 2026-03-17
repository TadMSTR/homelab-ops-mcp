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

Streamable-HTTP on `http://0.0.0.0:<port>/mcp` (default port 8282).

For LibreChat agents running in Docker, the host is reachable at `http://host.docker.internal:8282/mcp`. Note: `host.docker.internal` bypasses LibreChat's SSRF guard (which blocks private IP ranges) — use this hostname rather than `127.0.0.1`.

## Prerequisites

```bash
pip3 install fastmcp psutil
```

## Running

```bash
python3 server.py                        # default host 0.0.0.0, port 8282
python3 server.py --port 9090            # custom port
python3 server.py --host 127.0.0.1       # local-only (more restrictive)
```

With PM2:
```bash
pm2 start server.py --name homelab-ops-mcp --interpreter python3 -- --port 8282
pm2 save
```

## MCP Client Configuration

### LibreChat (`librechat.yaml`)

```yaml
mcpServers:
  homelab-ops:
    type: streamable-http
    url: http://host.docker.internal:8282/mcp
```

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
