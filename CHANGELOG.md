# Changelog

## [Unreleased]

### Fixed

- **File tools silently mis-wrote tilde paths.** `read_file`, `write_file`,
  `edit_file` and `read_directory` passed their `path` argument straight to
  `Path()`, which does no tilde expansion. A path like `~/notes/x.md` is therefore
  not absolute, so it resolved against the server's working directory: the tool
  created a literal `~` directory tree there and returned success. All four tools
  now validate that `path` is absolute and reject it otherwise, with the offending
  path echoed in the error. A separate backstop refuses any path whose components
  include a literal `~`, even when the path as a whole is absolute.

  Rejecting rather than expanding is deliberate — every one of these tools already
  documented its argument as "Absolute path to the file", so this enforces the
  stated contract instead of quietly widening it.

  `run_command`'s `cwd` argument gets the same validation, and now fails before
  spawning a shell rather than surfacing a bare `ENOENT`. Tilde paths *inside* the
  command string are untouched; those go through bash, which expands them correctly.

### Added

- **Child process environment allowlist for `run_command`.** The environment handed to
  a shelled-out child can now be restricted to an explicit allowlist —
  `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `LANG`, `TZ` and the POSIX `LC_*`
  variables — extended with exact names via `SYSTEM_OPS_CHILD_ENV_ALLOWLIST`. Previously
  the child inherited the server's whole environment apart from three PM2 IPC variables,
  which meant anything the server was started with reached every command it ran, whether
  or not that command had any use for it.

  It ships **off by default**, in shadow mode: behaviour is unchanged, but
  `run_command`'s `.done`, `.timeout` and `.error` log records now carry
  `env_withheld_count` (how many variables enforcement would remove) and `env_enforced`.
  Of the environment itself only the count is logged — never a variable name, never a
  value.

  Because that count describes the server's environment rather than any one command, it
  is identical on every call and so cannot tell you which callers enforcement would break.
  A second record, `run_command.env_referenced_withheld`, does: it is emitted only when a
  command reads a variable the allowlist would remove, and it names them. Those names come
  from the command text the caller wrote, not from the environment. Collecting them over a
  representative workload gives you the `SYSTEM_OPS_CHILD_ENV_ALLOWLIST` to set before
  turning enforcement on.

  `SYSTEM_OPS_CHILD_ENV_ALLOWLIST` accepts exact names only. Glob patterns are refused
  and logged rather than matched, since a prefix silently admits every future variable
  sharing it. The PM2 IPC variables stay excluded in both modes and cannot be re-added
  through the allowlist.

- **`read_multiple_files(paths)`** — read several files in one call. Each path gets its own
  result, so one bad path does not fail the rest, and the response carries `ok`/`failed`
  counts. `read_file` was the second most-called tool here and agents were paying a round
  trip per file.

- **`dry_run` on `edit_file`** — reports what the edit would do, including the size change,
  and writes nothing. `dry_run` is on every `edit_file` response so its absence and its
  falsity are not the same thing.

- **Closest-match feedback on a failed `edit_file`.** A failed match returned only a count,
  which says something is wrong but not what — and an agent faced with that tends to
  abandon the edit and rewrite the whole file, a far larger and riskier write than the one
  it was attempting. The response now carries the most similar passage in the file, its line
  number, a similarity score, and an `ndiff` whose `?` lines mark the differing *characters*.
  Nothing is reported when the file is over 2 MiB or when nothing resembles the request
  closely enough to be worth quoting — a confident wrong guess is worse than none.

- **`execution_time`** on every `run_command` response — wall-clock seconds, on the success,
  timeout and error paths alike.

- **Optional telemetry** (`telemetry.py`, `[telemetry]` extra). OTLP traces and metrics,
  plus InfluxDB 3 and NATS sinks, each gated on its own environment variable and all off by
  default. No telemetry library is in the base dependencies and every backend import is
  lazy and guarded, so the base install is unaffected. Per tool: call count, error count
  and latency.

  A tool returning `{"error": ...}` counts as an error — these tools report failure by
  returning rather than raising, so counting exceptions alone would report a 0% error rate
  for a tool failing every call. A non-zero `exit_code` from `run_command` is not an error.
  The error label is a fixed `tool_error` rather than the message, which would be unbounded
  cardinality and would carry paths and command text off the host.

  The tools are synchronous, so the two network sinks run on a background event loop the
  module owns, started lazily and only when one of them is configured.

- **Tool annotations on all six tools.** `list_tools` now returns `readOnlyHint`,
  `destructiveHint`, `idempotentHint` and `openWorldHint`, so a client can distinguish a
  read from a write without matching on tool names. `read_file`, `read_directory` and
  `list_processes` are read-only; `write_file`, `edit_file` and `run_command` are
  destructive. `write_file` is idempotent and `edit_file` is not — a second identical edit
  finds no match and fails.

  `run_command` is the only tool with `openWorldHint: true`: it executes arbitrary shell,
  so the network is inside its envelope. The read-only tools omit `destructiveHint` and
  `idempotentHint` entirely, which the spec treats as meaningless when `readOnlyHint` is
  true.

- **Per-stream output cap on `run_command`.** stdout and stderr are each captured up to
  `SYSTEM_OPS_OUTPUT_LIMIT_BYTES` (default 1 MiB). Output is now read incrementally
  instead of buffered to EOF, so a command producing far more than that is stopped rather
  than held in memory and then handed to the caller in full. On a breach the response sets
  `truncated: true` and the captured text carries an explicit `[truncated: …]` marker —
  `truncated` is present on every response so a capped result can be told apart from a
  short one.

- Release workflow (`.github/workflows/release.yml`): a tag push cuts a GitHub
  Release. (Was added in `72232a6` and not recorded here at the time.)

### Changed

- **The log stream is now JSON end to end.** Records from uvicorn, fastmcp and the MCP SDK
  go through the same structlog processor chain as this server's own events instead of
  being written as plain text beside them, and every record carries a `logger` field
  naming its source. fastmcp sets `propagate = False` on its logger and attaches Rich
  handlers, which also meant its exception tracebacks — the records that matter most when
  something is wrong — never reached the JSON stream at all; that logger is now reclaimed.

- **uvicorn's per-request access log is off.** One line per request, on a server whose
  request volume is entirely MCP tool traffic, saying nothing the per-tool events do not
  already record. Measured on one rotated day-file: 154,109 access lines against 15,089
  structured events, so 91% of the file was untyped noise. uvicorn *errors* still log.

- The FastMCP startup banner is suppressed — 18 lines of ASCII art per start, on a
  headless service whose log is read with `grep`.

- `run_command` now kills the child by process group rather than killing `bash` alone.
  A timeout previously reaped the shell but left the commands it had spawned running.

- `ecosystem.config.js` now declares its `env` block explicitly rather than omitting
  it, so "no environment" reads as an assertion instead of an accident. (Was changed
  in `acc8c35` and not recorded here at the time.)

## [0.2.1] — 2026-07-20

### Security

- **[Low]** `ecosystem.config.js` now runs the server through `run-hardened.sh`, a
  wrapper that sets `ulimit -c 0` before exec'ing the venv python3 process. PM2
  fork_mode has no native ulimit option, so a crash could previously leave a core
  dump on disk (a stray 37MB dump was found and removed during HLOPS-1 remediation).
  Remainder of the `homelab-ops-mcp-v02` audit LOW deferred from the v0.2.0 deploy.

## [0.2.0] — 2026-07-16

Repo brought up to the current forge MCP repo standard. No tool behavior changes —
same six tools, same streamable-http transport, same wire contract.

### Added

- `pyproject.toml` — packaged with setuptools (`src/` layout), a `homelab-ops-mcp`
  console entry point, and a `dev` extra (pytest, pytest-cov, ruff, pip-audit).
- Test suite under `tests/` with 48 tests at ~96% coverage, including a regression
  test for the HLOPS-1 PM2 IPC env leak.
- GitHub Actions CI (`.github/workflows/ci.yml`): Python 3.11/3.12/3.13 matrix,
  SHA-pinned actions, `ruff check`, `ruff format --check`, `pytest --cov`, and
  `pip-audit --strict`.
- Structured JSON logging via `structlog` (on by default; `LOG_LEVEL` and `LOG_FILE`
  env vars). Command text and file contents are logged at DEBUG only.
- `ARCHITECTURE.md` and `CONTRIBUTING.md`.

### Changed

- Server code moved from a flat `server.py` into the `homelab_ops_mcp` package under
  `src/`. A thin root `server.py` shim is retained so the existing
  `python server.py --host … --port …` PM2 invocation keeps working after an editable
  install (`pip install -e .`).
- Expanded `.gitignore` to the standard set.

### Security

Remediates the `homelab-ops-mcp-v02` audit (1 Medium, 2 Low; no blockers):

- **[Medium]** `--host` now defaults to `127.0.0.1` instead of `0.0.0.0`. This server
  offers unauthenticated shell execution and filesystem access, so loopback is the safe
  default; pass `--host 0.0.0.0` explicitly to expose it (e.g. container reachability).
  The PM2 deploy already passed `127.0.0.1` explicitly, so there is no deploy-behavior change.
- **[Low]** `write_file` and `edit_file` now write atomically (temp file + `os.replace`),
  so a crash mid-write can no longer leave a truncated file. Existing file modes are preserved;
  newly created files keep a private `0600` mode.
- **[Low]** Removed a stray pre-fix core dump from the working tree and documented
  `ulimit -c 0` guidance for the deploy.

### Deploy note

- Deploying this version requires `pip install -e .` in the server's venv (installs the
  `homelab_ops_mcp` package) before restarting the PM2 process. Supersedes v0.1.1.

## [0.1.1] — 2026-07-16

### Fixed

- `run_command` now strips PM2's IPC environment variables (`NODE_CHANNEL_FD`,
  `NODE_CHANNEL_SERIALIZATION_MODE`, `NODE_UNIQUE_ID`) before spawning children.
  Under PM2 these leaked into shelled-out processes, causing any Node.js child
  (node, pnpm, tsc, etc.) to inherit a stray file descriptor and SIGABRT during
  teardown — 100% reproducible via `run_command`, 0% via a direct shell. (HLOPS-1)

## [0.1.0] — 2026-03-10

### Added

- Initial release of `homelab-ops-mcp` — shell and file access MCP server for the homelab ops agent
- `run_command(command, cwd, timeout)` — Execute shell commands via `bash -c`; returns stdout, stderr, exit_code
- `read_file(path)` — Read a file from the local filesystem
- `write_file(path, content)` — Write a file to the local filesystem
- `list_directory(path)` — List directory contents
- `get_processes(name_filter)` — Inspect running processes via psutil
- FastMCP streamable-http transport on configurable port
- Structured logging via structlog; optional InfluxDB and NATS telemetry (off by default)
- PM2 ecosystem config for forge deployment
