#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
PYTHON_BIN="${ATLAS_PYTHON:-python3}"
exec "$PYTHON_BIN" -m uvicorn app.main:app --host "${ATLAS_HOST:-127.0.0.1}" --port "${ATLAS_PORT:-8000}"
