# homelab-ops-mcp

[![Built with Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-6B57FF?logo=claude&logoColor=white)](https://claude.ai/code)
[![CI](https://github.com/TadMSTR/homelab-ops-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/TadMSTR/homelab-ops-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A [FastMCP](https://github.com/jlowin/fastmcp) server that provides shell command execution, file system access, and process inspection over the [MCP Streamable-HTTP transport](https://modelcontextprotocol.io/specification/). Built for homelab operations agents that need to read files, run commands, and inspect processes on the host machine.

## Security Warning

This server provides **unrestricted shell access** to the host machine. Any MCP client with a connection can run arbitrary commands, read any file the process user can access, and write to any writable path. Deploy it only behind a trusted network boundary and on services you control. It is not designed for multi-user or internet-exposed environments.

## Tools

| Tool | Description |
|------|-------------|
| `run_command` | Execute a shell command (`bash -c`), returns stdout/stderr/exit_code |
| `read_file` | Read a file by absolute path, optional `start_line`/`end_line` |
| `read_multiple_files` | Read several files in one call; one failure does not fail the rest |
| `write_file` | Write/overwrite a file, creates parent dirs by default |
| `edit_file` | Find-and-replace edit — `old_str` must match exactly once; `dry_run` supported |
| `read_directory` | List directory contents, optional recursive with `max_depth` |
| `list_processes` | List running processes sorted by cpu/mem/pid, optional name filter |

## Tool annotations

Every tool publishes MCP annotations, so a client can tell a read from a write without
maintaining a list of tool names:

| Tool | readOnlyHint | idempotentHint | destructiveHint | openWorldHint |
|---|---|---|---|---|
| `read_file` | true | – | – | false |
| `read_multiple_files` | true | – | – | false |
| `read_directory` | true | – | – | false |
| `list_processes` | true | – | – | false |
| `write_file` | false | true | true | false |
| `edit_file` | false | false | true | false |
| `run_command` | false | false | true | **true** |

`run_command` is the only `openWorldHint: true` tool — it runs arbitrary shell, so the
network is reachable through it. The read-only tools omit the two write hints, which the
spec treats as meaningless when `readOnlyHint` is true.

## Transport

Streamable-HTTP on `http://<host>:<port>/mcp` (default `127.0.0.1:8282`).

The bind host defaults to `127.0.0.1` (loopback only). Because this server offers
**unauthenticated** shell execution and filesystem access, network isolation is its
control — keep it loopback-bound unless you have a specific reason to expose it.

### Host and Origin check

Loopback alone does not keep a browser out. A web page whose hostname an attacker points
at `127.0.0.1` (DNS rebinding) is, to the browser, same-origin with this port. The server
therefore refuses any request whose `Host` header is not a loopback name, with `421`, and
any request that carries an `Origin` header at all, with `403`:

| Accepted `Host` | Accepted `Origin` |
|---|---|
| `127.0.0.1:*`, `localhost:*`, `[::1]:*` | none: only browsers send `Origin`, and no browser is a client |

Any port is accepted, not only the bind port: a client that reaches the server through a
port forward (an SSH tunnel, say) sends the forward's local port. MCP clients send no
`Origin` and are unaffected by that half. Loopback origins are refused too: allowing them
would trust every web app served on the same host.

If you bind `--host 0.0.0.0`, the clients arrive under another name and are refused until
you list it:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SYSTEM_OPS_ALLOWED_HOSTS` | _(unset)_ | Comma-separated **exact** extra `Host` values, e.g. `host.docker.internal:8282` |

If an MCP client runs in its own container or network namespace and must reach the server
across it, bind `--host 0.0.0.0` explicitly — loopback isn't reachable from another
namespace. Do this only on a trusted, isolated network, and be aware that a container
reaching the host via `host.docker.internal` bypasses any SSRF guard that blocks private
IP ranges.

## Installation

```bash
uv sync --frozen --no-dev --no-editable          # a deployment: exactly the audited uv.lock
uv sync --frozen --no-dev --no-editable --extra telemetry   # ... with telemetry
pip install .                                     # ad hoc: re-resolves, so NOT the lock
```

This installs the `homelab_ops_mcp` package and a `homelab-ops-mcp` console script.
CI tests and audits the versions in `uv.lock`; `pip install .` resolves the ranges in
`pyproject.toml` afresh and can install something CI never saw.

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

### Telemetry

Off by default and entirely optional. The base install carries no telemetry libraries at
all, and every backend import is lazy and guarded, so an unset variable or a missing
package degrades to a no-op rather than an error.

```bash
pip install ".[telemetry]"
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset)_ | OTLP gRPC collector for traces and metrics |
| `OTEL_SERVICE_NAME` | `homelab-ops-mcp` | Service name reported to the collector |
| `SYSTEM_OPS_INFLUXDB3_URL` | _(unset)_ | InfluxDB 3 write endpoint |
| `SYSTEM_OPS_INFLUXDB3_TOKEN` | _(empty)_ | InfluxDB 3 token |
| `SYSTEM_OPS_INFLUXDB3_DATABASE` | `homelab_ops_mcp` | InfluxDB 3 database |
| `SYSTEM_OPS_NATS_URL` | _(unset)_ | NATS server for metric events |
| `SYSTEM_OPS_NATS_SUBJECT` | `system.ops.mcp.metrics` | NATS subject |

Per tool: call count, error count, and latency. A tool that returns `{"error": ...}` counts
as an error — these tools report failure by returning rather than raising, so counting
exceptions alone would show a 0% error rate for a tool failing every call. A non-zero
`exit_code` from `run_command` is *not* an error: the command failed, the tool worked.

The error label is a fixed `tool_error`, never the message. The messages embed paths and
command text, which is unbounded cardinality for a metrics backend and content that should
not leave the host.

The tools here are synchronous, so the two network sinks run on a background event loop
this module owns. It is started lazily and only if InfluxDB or NATS is configured — with
both unset, no thread is created.

**Verifying it works.** A clean `force_flush()` return is not evidence: it returns `True`
against an unreachable collector too. What distinguishes them is the exporter's own
records — a wrong endpoint logs `Failed to export traces …`. Check for the absence of
those, or better, check the service appears by name in your backend.

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
treatment, and the timeout bounds the command's exit, not only its output: a command that
redirects its own stdio (`exec >>"$LOG" 2>&1`) is still killed at the deadline.

Every `run_command` response has the same keys: `stdout`, `stderr`, `exit_code`,
`truncated`, `execution_time` and `error`. `error` is `null` unless the tool itself failed
(a refused `cwd`, or a command that could not be started); a non-zero exit code or a
timeout is the command's outcome and leaves it `null`.

### File tools

| Behaviour | Detail |
|---|---|
| Read cap | `read_file` returns at most `SYSTEM_OPS_OUTPUT_LIMIT_BYTES` of content, counted as UTF-8 after decoding; past that `truncated` is `true` and `returned_lines` says where it stopped. The file is streamed and reading stops once the range or the budget is done, so a short range costs a short read; `total_lines` is then `null`, since the rest was not counted. `read_multiple_files` shares one budget across the whole call and counts each distinct path once. |
| Line ranges | `start_line` below 1 is read as 1. `end_line` below 1, or before `start_line`, is an error. A range starting past the end returns empty content with `returned_lines: "none"`. Lines end at LF, as for `wc -l`. |
| Byte fidelity | `read_file` returns content as it is on disk, CRLF included. `edit_file` rewrites only the matched span; every other byte is kept. In a CRLF file an `old_str` written with LF breaks is matched against its CRLF form and `new_str` is given CRLF breaks (`line_endings: "crlf"` in the result). A file that is not valid UTF-8 is refused by `edit_file` rather than re-encoded. |
| Symlinks | `write_file` and `edit_file` write *through* a symlink: the link is kept, its target changes, and `written_to` names the file written. A link that another user planted in a shared sticky directory such as `/tmp`, anywhere in the path, is refused, as the kernel's `protected_symlinks` rule would. With `create_dirs`, a missing directory the link points into is created. |
| Hard links | A file with more than one hard link is refused by `write_file` and `edit_file`. Their writes are atomic (temp file and rename), and a rename would detach this name from the others. |
| What `edit_file` reads | Regular files only, up to 16 MiB. A FIFO, device or directory is refused without blocking, and so is a larger file; use `run_command` for those. |
| No-match hint | When `edit_file` finds no match it quotes the closest passage. That search is skipped for an `old_str` over 2 KiB or a file over 2 MiB, and gives up after one second, so a failed edit always returns promptly. |

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

`run_command.done`, `.timeout` and `.error` records carry `env_withheld_count`,
`env_enforced` and `verb_class`. For the environment itself only the count is recorded —
never a variable name, never a value. The separate `run_command.env_referenced_withheld`
record does carry names, but only ones the caller wrote into the command text.

Command text and file contents are logged at `DEBUG` only; `INFO` records carry
non-sensitive metadata (paths, cwd, exit codes).

`verb_class` says whether the command was a version-control operation, without saying
what it was:

| Value | Meaning |
|-------|---------|
| `git_write` | a `git` subcommand that changes local or remote state |
| `gh_write` | a `gh` subcommand that changes state, including `gh api` with a non-GET method |
| `git_read` | a read-only `git` subcommand |
| `gh_read` | a read-only `gh` subcommand, including a plain `gh api` GET |
| `vcs_other` | a `git`/`gh` invocation whose subcommand is not classified |
| `unclassified` | the command was too long to parse (see below) — *not* a claim that it was harmless |
| `other` | not `git` or `gh` at all |

It is derived from the parsed command at the same point the `DEBUG` record already has
it. A compound command takes the class of its most significant segment, so
`git status && git push` is `git_write`.

It sees through the ways one command hides inside another:

| Form | Example |
|---|---|
| wrappers and `sudo` | `sudo git reset --hard` |
| absolute paths | `/usr/bin/git push` |
| leading assignments | `GIT_DIR=/a git -C /b push` |
| pipelines and `&&`/`;` chains | `git status && git push` |
| newlines | `cd /r`⏎`git push` |
| command substitution | `echo $(git push)`, `` x=`git push` `` |
| shell `-c` payloads | `bash -c "git push"` |
| positional wrapper args | `timeout 30 git push`, `ssh host git push` |
| command-bearing flags | `find . -exec git push {} \;` |
| nested git execution | `git submodule foreach 'git push'` |

Where a subcommand is ambiguous the classification resolves toward `write`:
under-reporting a write would hide exactly what the field exists to show. That includes
subcommands whose bare form reads harmless but is not — `git stash` *is* `git stash push`,
`git reflog expire` destroys history, `git bisect start` moves `HEAD`.

`vcs_other` is deliberately distinct from `other` — a `git` subcommand nobody has
classified is a very different fact from `ls`, and a rising `vcs_other` is the signal the
table needs a new entry.

`unclassified` is distinct from both. Commands longer than `MAX_CLASSIFY_BYTES` (64 KiB)
are parsed only up to that bound, because `shlex` is quadratic on one long unbroken token
and classification runs *before* `run_command`'s own `timeout` can apply — so an oversized
command would otherwise hang the call at the classification step regardless of `timeout`.
If a write is found in the parsed head it is still reported; if nothing is found the answer
is `unclassified`, never `other`. `other` is a positive claim that this was not a repo
operation, and a parse that was declined cannot support it.

Everything on the stream is JSON, including records from uvicorn, fastmcp and the MCP SDK
— they are routed through the same processor chain rather than writing plain text
alongside it. Each record carries a `logger` field naming its source, so this server's own
events and a library's are distinguishable.

uvicorn's per-request access log is off. It was one line per request with the request
volume being entirely MCP tool traffic, and it said nothing the per-tool events do not.
uvicorn *errors* still log.

There is no rotation built in — point `LOG_FILE` at a path your log rotation already
covers. The handle is opened in append mode, so a `copytruncate`-style rotation is safe.

## Development

```bash
uv sync --extra dev            # installs exactly uv.lock
uv run ruff check . && uv run ruff format --check .
uv run pytest --cov=homelab_ops_mcp --cov-report=term-missing
```

Dependencies are locked in `uv.lock`, and CI installs from it, so a green run says
something about the versions that are deployed. CI runs the suite across Python 3.11–3.13,
once more with the `[telemetry]` extra installed, and once against the newest versions
`pyproject.toml` allows. It audits the locked set, not a fresh resolve.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow and [ARCHITECTURE.md](ARCHITECTURE.md)
for the layout.

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
