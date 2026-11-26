#!/usr/bin/env bash
# Serve the UI and /api from one process for a hosted preview.
#
# `npm run dev` binds Vite to 127.0.0.1 and the dev bundle points the browser at
# http://127.0.0.1:8000, so outside a developer's laptop every build call hits the
# visitor's own machine and the room is dead. The production build is served by the
# harness itself (main.py mounts dist/ at `/`), which makes the API same-origin, so
# this is the shape that works behind a proxy.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  bash scripts/setup.sh
fi
# Always rebuild: the harness serves this bundle, so a stale dist/ would show an old UI.
npm run build

export PYTHONPATH="${PYTHONPATH:-src}"
# Accounts, the roster keyring and the run token live here, outside the run workspaces.
mkdir -p workspace/runs logs

exec ./.venv/bin/python -m uvicorn ai_harness.main:app --host 0.0.0.0 --port "${PORT:-8000}"
