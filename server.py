"""Backwards-compatible entry shim.

The server now lives in the ``homelab_ops_mcp`` package under ``src/``. This shim
preserves the historical ``python server.py --host ... --port ...`` invocation
(used by run-hardened.sh). It imports the installed package, so it works after an
editable or a non-editable install (``uv sync --frozen --no-dev --no-editable``).
Prefer the ``homelab-ops-mcp`` console script for new deployments.
"""

from homelab_ops_mcp.server import main

if __name__ == "__main__":
    main()
