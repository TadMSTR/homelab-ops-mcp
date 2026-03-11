# homelab-ops-mcp

FastMCP server providing shell and file access for a LibreChat homelab ops agent on claudebox.

## Tools

| Tool | Description |
|------|-------------|
| `run_command` | Execute a shell command (bash -c), returns stdout/stderr/exit_code |
| `read_file` | Read a file by path, optional start_line/end_line |
| `write_file` | Write/overwrite a file, creates parent dirs by default |
| `edit_file` | Find-and-replace edit — old_str must match exactly once |
| `read_directory` | List directory contents, optional recursive with max_depth |
| `list_processes` | List running processes sorted by cpu/mem/pid, optional name filter |

## Transport

Streamable-HTTP on `http://0.0.0.0:8282/mcp`
LibreChat reaches it via `http://host.docker.internal:8282/mcp`

## PM2

```
pm2 name:  homelab-ops-mcp
pm2 id:    11
command:   python3 /home/ted/repos/personal/homelab-ops-mcp/server.py --port 8282
```

Start/stop:
```bash
pm2 restart homelab-ops-mcp
pm2 logs homelab-ops-mcp --lines 50
```

## Dependencies

```bash
pip3 install fastmcp psutil --break-system-packages
```

## LibreChat wiring

Add to `librechat.yaml` under `mcpServers:`:

```yaml
mcpServers:
  homelab-ops:
    type: streamable-http
    url: http://host.docker.internal:8282/mcp
```

No `allowedDomains` needed — `host.docker.internal` resolves to the Docker gateway,
not a private IP range, so LibreChat's SSRF guard doesn't block it.

## Deploy script note

`deploy-claudebox.sh` must install fastmcp and psutil before `pm2 resurrect`:
```bash
pip3 install fastmcp psutil --break-system-packages
```
