#!/usr/bin/env bash
# One command that runs every check. Named in AGENTS.md, wired into the Stop hook in
# .claude/settings.json, and the same command you run by hand. If these three drift
# apart, checks start passing locally and failing in review.
set -uo pipefail

cd "$(dirname "$0")/.."

# Resolve the tools, then say plainly what is missing. An empty command here used to
# run as `if ! ; then`, so a machine without ruff reported a shell error instead of
# "ruff is not installed" and the lint stage silently never ran.
PY_VERSION="$(cat .python-version 2>/dev/null || true)"

RUFF=(./.venv/bin/ruff)
[ -x "${RUFF[0]}" ] || RUFF=("$(command -v ruff || true)")

# The pinned interpreter first, then the same pin through uv or pyenv, then whatever
# `python3` happens to be. `.python-version` is the project's answer; a bare `python3`
# on a machine with 3.15 installed is not.
PY=(./.venv/bin/python)
if [ ! -x "${PY[0]}" ]; then
  if command -v uv >/dev/null 2>&1 && [ -n "$PY_VERSION" ]; then
    PY=(uv run --python "$PY_VERSION" python)
  elif command -v pyenv >/dev/null 2>&1 && [ -n "$PY_VERSION" ]; then
    PY=(pyenv exec python)
  else
    PY=("$(command -v python3 || true)")
  fi
fi

missing=()
[ -n "${RUFF[0]}" ] || missing+=("ruff")
[ -n "${PY[0]}" ] || missing+=("python3")
if [ ${#missing[@]} -gt 0 ]; then
  printf 'missing: %s\nRun scripts/setup.sh first — it creates .venv and installs ruff, pytest and the harness deps.\n' "${missing[*]}" >&2
  exit 1
fi

# A tool that exists but has no pytest is the same failure one step later, with a
# ModuleNotFoundError instead of an instruction. Catch it here.
if ! "${PY[@]}" -c 'import pytest' >/dev/null 2>&1; then
  printf 'pytest is not installed for: %s\nRun scripts/setup.sh first (it installs requirements-dev.txt).\n' "${PY[*]}" >&2
  exit 1
fi

failed=()
run() {
  local name="$1"
  shift
  printf '\n== %s ==\n' "$name"
  if ! "$@"; then
    failed+=("$name")
  fi
}

run "lint (ruff check)" "${RUFF[@]}" check .
run "format (ruff format --check)" "${RUFF[@]}" format --check .
run "test (pytest)" "${PY[@]}" -m pytest -q
# Scripted, offline, against the saved baseline: a prompt or verifier change that makes a
# golden case worse is a failure of this command, not something a human notices later.
# Promoted regressions replay here too, through the verifier only, with no model call.
run "evals (scripted, vs baseline)" env PYTHONPATH=src "${PY[@]}" -m ai_harness.evals --compare
run "types (tsc)" npm run --silent typecheck

if [ ${#failed[@]} -gt 0 ]; then
  printf '\nFAILED: %s\n' "${failed[*]}" >&2
  exit 1
fi
printf '\nall checks passed: lint, format, tests, evals, types\n'
