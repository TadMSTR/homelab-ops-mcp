# Contributing

## Development setup

```bash
git clone https://github.com/TadMSTR/homelab-ops-mcp.git
cd homelab-ops-mcp
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running checks

The same four checks run in CI (across Python 3.11–3.13):

```bash
ruff check .
ruff format --check .
pytest --cov=homelab_ops_mcp --cov-report=term-missing
pip-audit --strict .
```

Coverage floor is 80% (`fail_under` in `pyproject.toml`); the suite currently
sits well above it.

## Conventions

- Python 3.11+, type annotations throughout (`X | None`, not `Optional[X]`).
- `structlog` for logging — command text and file contents at `DEBUG` only; never
  log secrets.
- Tools are plain functions decorated with `@mcp.tool`; keep them importable and
  directly callable so they stay unit-testable.
- Branch before editing — do not commit directly to `main`. Open a PR; CI must be
  green before merge.

## Adding a tool

1. Add the function to `src/homelab_ops_mcp/server.py`, decorated with `@mcp.tool`,
   returning a JSON-serialisable `dict`.
2. Add tests under `tests/` covering the success and error paths.
3. Update the tool table in `README.md` and the tool list in `AGENTS.md`.
4. Add a `CHANGELOG.md` entry.
