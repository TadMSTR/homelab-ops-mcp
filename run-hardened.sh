#!/bin/bash
# PM2 wrapper: disable core dumps before exec'ing the server.
# PM2 fork_mode has no native ulimit option, so this shell shim sets it
# and hands off to the venv interpreter (HLOPS-1 audit LOW finding).
set -euo pipefail
ulimit -c 0
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv/bin/python3" "$DIR/server.py" "$@"
