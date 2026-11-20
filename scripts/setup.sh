#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
# The verification loop: ruff and pytest are dependencies, not assumptions.
./.venv/bin/pip install -r requirements-dev.txt

if [ ! -d node_modules ]; then
  npm install
fi

mkdir -p workspace/runs logs
echo "ready — start the harness with:"
echo "  PYTHONPATH=src ./.venv/bin/python -m uvicorn ai_harness.main:app --host 127.0.0.1 --port 8000"
echo "  npm run dev"
