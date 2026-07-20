# Changelog

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
