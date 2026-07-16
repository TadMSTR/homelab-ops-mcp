# Changelog

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
