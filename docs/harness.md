# The harness contract

A model is a pure function: tokens in, tokens out. Everything that makes it finish a task
— the environment it can act in, what it can see, what checks it, what it may never touch,
and what survives the run — is the harness. This document is the map. The root
`AGENTS.md` stays short and points here; code comments carry the local reasons.

Sources this design follows, in reading order:

1. Mitchell Hashimoto, *My AI Adoption Journey* — engineer the mistake away, do not
   explain it better.
2. OpenAI, *Harness engineering: leveraging Codex in an agent-first world* — the
   repository is the system of record; make the application legible; enforce invariants.
3. Anthropic, *Effective harnesses for long-running agents* — initializer, feature list,
   incremental progress, progress file, end-to-end verification.
4. Anthropic, *Harness design for long-running application development* — separate the
   generator from the evaluator; grade against criteria, not vibes.
5. Manus, *Context Engineering for AI Agents* — stable prefix, filesystem as memory,
   recitation, keep failures in context, do not few-shot yourself into a rut.
6. Aishwarya Srinivasan, *Build Your First Agent Harness* — the five components and the
   15-point self-audit.

## The five components, and where they live

| Component | Question | Where |
| --- | --- | --- |
| System of record | What does it need to know? | `AGENTS.md` (map), `docs/` (detail), `README.md`, prompt fingerprints |
| Tools | What can it actually do? | `src/ai_harness/tools/` + `modules/runner.py`, `modules/terminal`, `modules/web_search` |
| Feedback and verification | How does it know it worked? | Ruff, tests, `nodes/verify.py`, `modules/toolchain.py`, `modules/probe.py`, `evals/` |
| Guardrails and permissions | What must it never do? | `modules/permissions.py`, origin/token check, separate preview origin, approval checkpoint |
| Observability and memory | What happened, what carries over? | `events.jsonl`, `logs/harness.log`, `failures.md`, `todo.md`, `memory.md`, metrics |

The components are not independent. A verifier the model cannot see is a suggestion; a
failure log nobody reads is a write-only file; a permission rule with no test is a wish.

## The reasoning contract

Reasoning is visible and separated, because the same model grading its own work reliably
grades it generously.

- **Build**: `REASON` (9 stages: restate, questions, options, decision, risks, blueprint,
  steelman, acceptance, self-check) → `REASON_CRITIC` by a *different* specialist (the
  reviewer) → `CODE` grounded in both. A `REVISE` finding is a requirement, not advice.
- **Chat**: red-team the answer, self-check the direction, then answer. The reasoning is
  streamed as `think` events; the answer never restates it.
- **Repair**: reviewer findings are fed back as the critique, so a rewrite is not a fresh
  guess.
- Every prompt is a constant in `modules/prompts.py`; its hash is recorded in
  `metrics.prompts`, so a wording regression is traceable to a run.

## The loop

`plan → scaffold → research → contracts → build → verify → review → repair → done`

- `done` is emitted only after the verifier passes; a failing check becomes a terminal
  `error`, never a green run with a warning.
- A run lives in its own `workspace/runs/<id>/` sandbox. `todo.md` is the checklist the
  agent re-reads; `events.jsonl` is the transcript; the file tree is the artifact.
- Work is bounded before it starts: token, dollar, wall-clock and concurrency budgets are
  checked before a call, not after it.

## Tools and blast radius

The policy is deny by default, then ask, then allow. Every capability has one row in
`docs/tool-inventory.md` naming why it exists and the worst thing it could do. A new
command or process needs a rule in `modules/permissions.py`; anything that runs code the
model wrote is `ask`, and the approval lists the exact commands before anything starts.
Provider keys never enter the browser; child processes get `permissions.safe_env`.
Generated pages run on a separate origin with no `/api`, under a per-run nonce.

## Memory

- **Provider keys** live in a `0600` keyring (`HARNESS_KEYS_FILE`), keyed by provider
  endpoint, not by a roster row. Replacing or resetting the roster cannot lose a key;
  only an explicit `clearKey` deletes one.
- **Project memory** (`modules/memory.py`) records decisions that should survive a run.
- **Failures** (`failures.md`) are one row per blocked run, promoted into
  `evals/regressions.jsonl` when they can become a check. A failure that never becomes
  a check will happen again.
- **Progress** (`todo.md`) is recitation: the objective is rewritten at the end of the
  context, where attention is strongest.

## Verification tiers

1. **Static**: Ruff lint and format, TypeScript types. Fast enough to run on every edit.
2. **Tests and evals**: pytest (no network, no provider), the offline eval suite, the
   structural verifier over the generated tree, the stack's own compiler.
3. **Live**: the runner starts the real service, `probe.py` fetches the page and follows
   its assets; the preview shows install/build/start failures with their own output.

`scripts/verify.sh` is the one command: lint, format, tests, evals, types. The `Stop` hook
runs it, so a turn cannot end on a broken build. A test or eval expectation is never edited
to make a run pass.

## Known gaps

- Live verification is an HTTP probe, not a browser driving the UI; interaction bugs
  still need a human or Playwright.
- The planner produces files and work units, not a persisted feature list with pass/fail
  flags. The verifier checks structure; it cannot check product intent.
- Context resets exist through per-run workspaces, but there is no cross-session handoff
  artefact yet: a new run starts from project memory, not from the last run's open work.
- A monthly subtraction pass exists (`scripts/audit.py`); it is not scheduled.
