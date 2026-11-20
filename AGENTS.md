# Project: Cortex — a chain-of-command harness

A team of models plans, researches, writes and reviews a small app together. The Python
harness runs the loop; the React UI shows the room.

## Structure

- `src/ai_harness/core/` orchestrator, run state and its `events.jsonl`, chat
- `src/ai_harness/nodes/` the stages: planning → research → contracts → building → review
- `src/ai_harness/agents/` one module per role; `modules/` the runtime guarantees
  (budget, cache, retry, llm, runner, permissions, guard); `tools/` file manager, web
  search, terminal
- `src/ai_harness/evals/` golden cases, the offline model script, baseline and results
- `src/pages/`, `src/components/`, `src/lib/` the UI. `src/lib/harness.ts` types the
  server's event contract — change an event, change it there too.
- `tests/` pytest files, one per area, no network and no provider
- `docs/architecture.md` the pipeline, event contract and guarantees; read it before
  changing a node. `docs/harness.md` the harness contract — the five components, the
  reasoning loop and the known gaps. `docs/tool-inventory.md` the blast radius of each
  capability.
- `scripts/` `verify.sh` (the one check), `setup.sh` (install), `build_overview.py`
- `workspace/runs/<id>/`, `dist/`, `evals/results/` generated — never edited by hand

## Commands

Every command below has been run on this tree.

- Install: `bash scripts/setup.sh`
- Lint: `./.venv/bin/ruff check .`
- Format: `./.venv/bin/ruff format .` (check only: `ruff format --check .`)
- Types: `npm run typecheck`
- Test: `./.venv/bin/python -m pytest -q`
- **Verify — before you finish:** `bash scripts/verify.sh` (lint, format, tests, evals, types)
- Evals, offline and free: `PYTHONPATH=src ./.venv/bin/python -m ai_harness.evals --compare`
- Subtraction pass: `npm run audit` (stale docs, unreferenced prompts, failure components)
- Harness: `PYTHONPATH=src ./.venv/bin/python -m uvicorn ai_harness.main:app --host 127.0.0.1 --port 8000`
- UI: `npm run dev` (http://127.0.0.1:5178)
- Overview page from a real run: `./.venv/bin/python scripts/build_overview.py [run_id]`

The venv is Python 3.9, so a pydantic model field is `Optional[X]`, never `X | None`
(pydantic evaluates the annotation at import; see the `UP045` ignore in `pyproject.toml`).

## Conventions

Each line exists because something went wrong without it.

1. A new correctness rule goes in `nodes/verify.py::check()` and reaches the reviewer
   through `as_findings`, not into prompt wording: a model can ignore a sentence and
   cannot ignore a failing check.
2. Prompts are constants in `modules/prompts.py`. A string typed inline in a node loses
   its fingerprint in `metrics.prompts`, so a wording regression becomes untraceable.
3. Provider traffic goes through `llm.call` / `llm.stream`, which apply the budget,
   retry, cache and usage ledger. Direct `httpx` is only web fetch (`tools/web_search.py`)
   and the readiness probe in `modules/runner.py` — never a provider call.
4. Run files are written with `FileManager.write` / `scaffold`, which resolve paths
   inside the run workspace. Never `Path.write_text` a path under `workspace/runs/`.
5. A new command or process needs a rule in `modules/permissions.py`; the default is
   deny, and anything running code the model wrote is `ask`, not `allow`.
6. `done` is emitted only after the gate in `core/orchestrator.py` passes. Repair logic
   belongs in `nodes/review.py::repair`, never as a `done` emitted past a failed check.
7. Tests are pytest functions in `tests/test_<area>.py` that stub `llm` and `executor`
   instead of calling a provider, and assert events, files or refusals — not imports.
8. Changed a prompt or the planner? Run the evals. A case below `evals/baseline.json`
   is a regression; moving the baseline is its own deliberate commit.
9. A new setting is a default in `config/settings.py` plus a line in `.env.example`,
   because the README tells people to tune the harness there.
10. A sensor belongs in `nodes/verify.py` when it reads the tree, and in
    `modules/toolchain.py` when it needs the artifact's own compiler. A prompt is not a
    sensor; a `toolchain` note that says "skipped" is honest, a silent pass is not.
11. Anything that runs model-authored code goes through `modules/sandbox.py`. Do not add a
    spawn path that skips it, and never describe an unsandboxed process as contained.
12. A run may not edit its own judge: `modules/protected.py` freezes a test file once it
    has content, so a revision must fix the code instead of weakening the check.
13. Credentials live in the `0600` keyring (`modules/roster.py`, `HARNESS_KEYS_FILE`) by
    provider endpoint, never in a roster row. A roster save must not be able to delete a
    key; only an explicit `clearKey` does. This is why keys used to vanish on reset.
14. A deep plan is critiqued by a different specialist (`REASON_CRITIC`) before code is
    written, and the code prompt carries that critique. The author grading its own plan
    was the weakness; do not drop the pass to save one call.
15. A revision first tries `modules/agent_loop.py`: read, a targeted edit, check, repeat,
    with a check-gated `done`. The whole-file rewrite is the fallback when a model cannot
    speak the tool protocol, never the first choice. The prompt is `prompts.AGENT_REPAIR`,
    the state is rebuilt from disk each turn, and any write still goes through
    `FileManager` and `protected`.
16. Native provider tools are an optimisation, not the contract. `llm.call_tools` serves
    the OpenAI-compatible shape; a 4xx to `tools` disables it and the text protocol takes
    over. Do not add a second set of tool implementations for a second wire format.
17. A public share is `share.py` (one run, one path token, no `/api`) behind
    `modules/cloud.py` (approval, TTL, teardown). Never point a tunnel at `preview.py`,
    because that app serves every run by id.
18. A browser check is a post-serve sensor: it runs only after an approved preview is
    `ready`, reports through `run.browser` and a `verify` event, and never changes a
    run's terminal `done`.
19. Cloud execution is a separate service (`ai_harness/cloud_runner.py`), not a second
    route on `/api`. The harness submits a tar.gz with a server-to-server token; the
    runner has no roster, provider keys or `/api`, and returns a per-run capability URL.
    A new cloud provider must keep that boundary rather than exposing the harness.
    Vercel (`modules/vercel_runner.py`) is the temporary static/Node provider: exact
    project name, SSO disabled for that project, removed at the TTL or on stop.

## Before you open a PR

`bash scripts/verify.sh` must pass — lint, format, tests and types — and the evals must
not fall below the baseline.

## Do not

- Edit or delete a test, or an `evals/cases.py` expectation, to make a check pass.
- Put a provider key in the browser, a log line, a roster row or a committed file. Keys
  live in the server-side keyring or the environment and are never read back to the client.
- Weaken a guardrail to make a demo work: the `/api` origin + token check, the token and
  dollar budget, the path sandbox, the process permissions.
- Add a dependency without asking; every install is surface area in a harness.
