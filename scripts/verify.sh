#!/usr/bin/env bash
# One command that runs every check. Named in AGENTS.md, wired into the Stop hook in
# .claude/settings.json, and the same command you run by hand. If these three drift
# apart, checks start passing locally and failing in review.
set -uo pipefail

cd "$(dirname "$0")/.."

RUFF="./.venv/bin/ruff"
[ -x "$RUFF" ] || RUFF="$(command -v ruff || true)"
PY="./.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"

failed=()
run() {
  local name="$1"
  shift
  printf '\n== %s ==\n' "$name"
  if ! "$@"; then
    failed+=("$name")
  fi
}

run "lint (ruff check)" "$RUFF" check .
run "format (ruff format --check)" "$RUFF" format --check .
run "test (pytest)" "$PY" -m pytest -q
# Scripted, offline, against the saved baseline: a prompt or verifier change that makes a
# golden case worse is a failure of this command, not something a human notices later.
# Promoted regressions replay here too, through the verifier only, with no model call.
run "evals (scripted, vs baseline)" env PYTHONPATH=src "$PY" -m ai_harness.evals --compare
run "types (tsc)" npm run --silent typecheck

if [ ${#failed[@]} -gt 0 ]; then
  printf '\nFAILED: %s\n' "${failed[*]}" >&2
  exit 1
fi
printf '\nall checks passed: lint, format, tests, evals, types\n'
