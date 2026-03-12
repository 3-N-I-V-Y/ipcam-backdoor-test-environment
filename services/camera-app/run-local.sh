#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export RUN_MODE="${RUN_MODE:-local}"

if [ -n "${PYTHON_BIN:-}" ]; then
  exec "$PYTHON_BIN" ./main.py
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 ./main.py
fi

exec python ./main.py
