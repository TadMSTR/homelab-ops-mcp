# Contributing

## Development setup

```bash
git clone https://github.com/TadMSTR/homelab-ops-mcp.git
cd homelab-ops-mcp
uv sync --extra dev            # add --extra telemetry to exercise the real backends
```

## Running checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=homelab_ops_mcp --cov-report=term-missing
```

CI runs these across Python 3.11–3.13 from `uv.lock`, again with the `[telemetry]`
extra, and once against the newest versions `pyproject.toml` allows. It also checks the
lock is in sync (`uv lock --check`), audits the locked set with `pip-audit`, and runs a
gitleaks secret scan (`python tests/check_gitleaks_gate.py` reproduces its self-test
locally, with gitleaks on `PATH`).

The coverage floor is `fail_under` in `pyproject.toml`; the comment above it carries the
measured figure and the date it was taken.

Changing a dependency: edit `pyproject.toml`, run `uv lock`, and commit both. CI fails a
`pyproject.toml` change that arrives without its lock.

## Conventions

- Python 3.11+, type annotations throughout (`X | None`, not `Optional[X]`).
- `structlog` for logging — command text and file contents at `DEBUG` only; never
  log secrets.
- Tools are plain functions decorated with `@mcp.tool`; keep them importable and
  directly callable so they stay unit-testable.
- Branch before editing — do not commit directly to `main`. Open a PR; CI must be
  green before merge.

## Adding a tool

1. Add the function to `src/homelab_ops_mcp/server.py`, decorated with
   `@mcp.tool(annotations={...})` and then `@_instrumented`, returning a
   JSON-serialisable `dict`. Report failure by returning `{"error": ...}`, which is
   what the telemetry counts.
2. Add tests under `tests/` covering the success and error paths.
3. Update the tool table in `README.md` and the tool list in `AGENTS.md`.
4. Add a `CHANGELOG.md` entry.
