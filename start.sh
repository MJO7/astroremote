#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--simulate" ]]; then
  export ASTROREMOTE_SIMULATE=1
  shift
fi

export ASTROREMOTE_PORT="${ASTROREMOTE_PORT:-8000}"

if [[ -x ".venv/bin/python" ]]; then
  exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$ASTROREMOTE_PORT" "$@"
fi

exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "$ASTROREMOTE_PORT" "$@"
