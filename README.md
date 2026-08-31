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

### Output limits

`run_command` captures at most `SYSTEM_OPS_OUTPUT_LIMIT_BYTES` (default 1 MiB) from
stdout and from stderr, counted separately. If either stream reaches the cap the child's
process group is killed, the captured output carries an explicit `[truncated: …]` marker,
and the response sets `truncated: true`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SYSTEM_OPS_OUTPUT_LIMIT_BYTES` | `1048576` | Per-stream capture cap, in bytes |

`truncated` is present on every `run_command` response, so a capped result is
distinguishable from a genuinely short one. Output is read incrementally rather than
buffered to EOF, so a command producing gigabytes is stopped rather than held in memory
first.

The child runs in its own session and is killed by process group, so the whole pipeline
goes rather than just the `bash` that spawned it. The timeout path gets the same
treatment — previously a timeout killed `bash` and left its children running.

### Child process environment

`run_command` shells out via `bash -c`. By default the child inherits the server's
environment, minus the PM2 IPC variables. That default is being narrowed to an explicit
allowlist, which is safer for any deployment whose server process carries configuration
the commands it runs have no need for.

The change ships in two steps so the effect can be measured before it is switched on.
Out of the box the server runs in **shadow mode**: behaviour is unchanged, but every
`run_command` log record carries `env_withheld_count` — how many variables enforcement
*would* remove.

That count on its own describes the server's environment rather than any particular
command, so it is the same on every call. The signal that tells you whether enforcement
would actually break a caller is `run_command.env_referenced_withheld`, logged only when a
command references a variable that enforcement would take away:

```json
{"event": "run_command.env_referenced_withheld", "names": ["MY_API_BASE"], "enforced": false}
```

Run shadow mode across a representative workload, collect the `names` from those records,
and they are your `SYSTEM_OPS_CHILD_ENV_ALLOWLIST`. Variable *names* are reported here
because they come from the command text the caller wrote; values are never read.

The detection is deliberately approximate and errs toward over-reporting — a `$VAR` inside
single quotes is counted even though the shell would not expand it, and an indirect read
such as `env | grep FOO` is missed.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SYSTEM_OPS_CHILD_ENV_ENFORCE` | `false` | `true` restricts the child to the allowlist. `false` reports the count without acting on it. |
| `SYSTEM_OPS_CHILD_ENV_ALLOWLIST` | _(unset)_ | Comma-separated **exact** variable names to add to the base allowlist. |

The base allowlist is `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `LANG`, `TZ`,
and the POSIX `LC_*` locale variables.

`SYSTEM_OPS_CHILD_ENV_ALLOWLIST` takes exact names only — glob patterns are refused and
logged rather than matched. A pattern would silently admit every future variable that
happens to share its prefix, which defeats the point of naming things explicitly.

Commands that read configuration from disk are unaffected: a
`source ./creds.env && …` runs the `source` *inside* the child shell, so the file is read
after the environment is set. What stops working under enforcement is a command relying on
a variable being ambient without sourcing it.

The PM2 IPC variables (`NODE_CHANNEL_FD`, `NODE_CHANNEL_SERIALIZATION_MODE`,
`NODE_UNIQUE_ID`) are removed in both modes and cannot be re-added through the allowlist —
a Node.js child that inherits them aborts during teardown.

### Logging

Structured JSON logs go to stderr by default. Tune via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `LOG_FILE` | _(unset)_ | Append logs to this path instead of stderr |

`run_command.done`, `.timeout` and `.error` records carry `env_withheld_count` and
`env_enforced`. For the environment itself only the count is recorded — never a variable
name, never a value. The separate `run_command.env_referenced_withheld` record does carry
names, but only ones the caller wrote into the command text.

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
