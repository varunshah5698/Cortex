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
| Tools | What can it actually do? | `src/ai_harness/tools/` + `modules/agent_loop.py` (read/edit/check repair), `modules/runner.py`, `modules/terminal`, `modules/web_search` |
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
- A revision is a **tool loop**, not a blind rewrite: the author reads the real file,
  makes a targeted edit, runs the deterministic checks and sees the result, bounded by
  `HARNESS_AGENT_STEPS`. `done` is refused while the current check fails; a model that
  cannot speak the protocol falls back to the old whole-file rewrite. The prompt is a
  flat state block rebuilt from disk each turn, so context stays flat and the ledger —
  not a chat transcript — is the memory.

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

0. **Design**: `modules/design.py` reads the written page and reports whether it was
   *designed* or only *wired* — classes the markup uses that no rule styles, a stylesheet
   whose selectors match nothing, a page with no styling at all, and the softer signals
   (no type scale, no tokens, no breakpoint, no `:hover`/`:focus-visible`). A dead
   stylesheet or an unstyled page is an **error**; under-design is a **warning** that
   becomes evidence in the reviewer's prompt and the repair loop. A framework tree is
   exempt from the hard gate, because its stylesheet legitimately lands after its
   components. The instruction that stops these findings existing is
   `prompts.DESIGN_CONTRACT`, injected into every code pass.
1. **Static**: Ruff lint and format, TypeScript types. Fast enough to run on every edit.

1.5. **Design**: `modules/design.py` asks whether the page was *designed* or only *wired*.

1.6. **Acceptance**: `modules/intent.py` asks the only question the person who asked
   actually has — *is this the thing I asked for?* The request's named capabilities are
   extracted into a checklist before any file is written, persisted beside the run as
   `acceptance.md`, injected into every code prompt, and then checked against the written
   tree for a trace of each. This is the tier that stops a weak model shipping a tidy,
   well-tokenised page containing none of the requested features — the failure mode every
   other tier passes. Errors are reserved for a build that ignores the request outright
   (no named capability present, or an interactive request with no script and no control);
   a single missing capability is a warning that names it, which is what the repair loop
   acts on. `HARNESS_INTENT_CHECK=0` restores structure-only verification.
2. **Tests and evals**: pytest (no network, no provider), the offline eval suite, the
   structural verifier over the generated tree, the stack's own compiler.

2.1. **Scope** (`modules/features.py`): `feature_list.json` in the run's workspace, seeded
   from the plan before any file is written. It enforces two rules the verifier cannot:
   one active feature at a time (`activate()` refuses a second), and `passing` reachable
   only through `pass_feature()` with a non-empty evidence list. A model that starts three
   things or claims a pass it cannot back is refused by the file, not asked nicely.

2.2. **Clean state** (`modules/cleanstate.py`): the end-of-session checklist answered from
   disk. An empty planned file and a feature marked passing without evidence are **errors**
   (they are false claims); a missing progress record or handoff is a **warning** (the run
   is usable, just less restartable). It runs after the verifier and before the goal gate,
   because it is mechanical and the goal gate may spend a model call.
   `HARNESS_CLEAN_STATE=0` turns it off.

2.3. **Rubric** (`modules/rubric.py`, opt-in via `HARNESS_RUBRIC`): six categories scored
   0-2 by a specialist that did not write the work, producing Accept / Revise / Block. The
   total decides the verdict; a model's stated verdict may only be *stricter*, never more
   generous, and a malformed answer is `unknown` rather than a pass. A `block` refuses the
   stop the same way an unmet goal does.

3. **Live**: the runner starts the real service, `probe.py` fetches the page and follows
   its assets; the preview shows install/build/start failures with their own output.
4. **Browser**: `modules/browser.py` opens the served page in headless Chrome, reports
   console/page/request errors and clicks up to two visible buttons. It is a post-serve
   verdict, recorded as `run.browser`; it never retroactively turns a `done` run green.

The **workflow graph** (`core/workflow.py`) is the control flow written down. `RunState.emit`
observes every `phase` event at the one choke point they all pass through, so the graph sees
the real sequence without a node knowing it exists. An edge the graph forbids is recorded on
the event as `graph_violation` rather than raised: an unexpected edge is worth reporting and
is not worth killing a build over. Self-edges are legal (`revising → revising` is a repair
pass re-entering its phase), and a run's first observed phase is always legal, so a resumed
run is not judged against an edge it never took.


`scripts/verify.sh` is the one command: lint, format, tests, evals, types. The `Stop` hook
runs it, so a turn cannot end on a broken build. A test or eval expectation is never edited
to make a run pass.

## Bounds on a single call

The run deadline (`runtime.check_deadline`) is checked *before* a provider call, which
stops work that has not started. It cannot stop work that has: a stream that stalls, or a
model that will not stop thinking, used to hold the run open until the two-hour wall-clock
limit. One review did exactly that — 9,256 reasoning events, 46 minutes, no terminal event,
and a room that looked alive the whole time. Three separate defects produced that one
symptom, so there are three separate bounds now:

- **`HARNESS_STREAM_SECONDS`** (180s) is checked inside `llm.stream` on every chunk. A call
  that outlives it is cut, `stats.truncated` is set, and a `stream-cut` event says the text
  is a prefix rather than a complete answer.
- **`HARNESS_REVIEW_THINK_CHARS`** (2,400) mutes the reviewer's reasoning past its budget.
  It mutes the *narration*, not the stream: breaking the generator would discard the
  findings, which arrive after the reasoning, and a model that over-thinks would lose its
  whole review.
- **`HARNESS_THINK_BATCH_CHARS` / `_MS`** coalesce reasoning deltas in `RunState.emit`, so
  one review writes tens of events instead of thousands. A non-think event flushes the
  buffer first, so the log's order still matches what happened.

## Known gaps

- A public share is a temporary one-run tunnel, not a deployment. It has a capability
  URL and a TTL, not accounts, per-user permissions or a custom domain; a real deployment
  still needs a host and its own auth.
- The browser tier drives Chrome over CDP and clicks up to two visible buttons. It is not
  a Playwright suite: there are no assertions about visual layout, accessibility or long
  interaction flows.
- The planner produces files and work units, not a persisted feature list with pass/fail
  flags. The verifier checks structure; it cannot check product intent. `modules/features.py`
  now keeps that list — one active feature, and `passing` reachable only through recorded
  evidence — but it is seeded from the plan, so it inherits the plan's notion of scope.
  Nothing yet *drives* the build from the list: a feature is marked passing by a caller,
  not by the verifier noticing it.
- The acceptance tier (`modules/intent.py`) narrows that gap but does not close it. It
  extracts capabilities from a lexicon of vocabulary, so a request phrased in words the
  lexicon does not know yields no criteria and no findings — the sensor is silent rather
  than wrong, which is the right failure, but it is still a miss. It tests for a *trace*
  of a capability, not its correctness: a page that mentions "search" and does nothing
  with it satisfies the criterion. Judging whether the search actually works stays with
  the browser tier and the reviewer.
- The design sensor reads CSS statically. It can prove a class is unstyled and that no
  breakpoint exists; it cannot see that the result is *ugly*, that the contrast is
  actually insufficient, or that the layout breaks at a width it never tested. Those stay
  with the browser tier and the reviewer.
- Context resets exist through per-run workspaces, and `modules/handoff.py` now writes a
  `session-handoff.md` per run beside the runs root, so a new run can read the last run's
  open work instead of starting from project memory alone. What is missing is the other
  half: a new run does not yet *consume* it. `handoff.latest()` exists and is tested; no
  node calls it at plan time, so the artefact is written and unread.
- The per-call bound is a wall-clock cut, not a token budget: a model that streams slowly
  but usefully for longer than `HARNESS_STREAM_SECONDS` is cut mid-answer. The setting is
  the knob; a per-model value would be better than one number for every provider.
- `tests/test_browser.py` drives real Chrome and is flaky on this machine (it skips when
  Chrome is busy and fails when it is slow). It is not part of the loop fix and it is not
  a regression: it fails the same way with the fix stashed.
- A monthly subtraction pass exists (`scripts/audit.py`); it is not scheduled.

## Tests may reach this machine, never the internet

`tests/conftest.py` installs an autouse `no_network` fixture that fails any connection
outside loopback. It exists because a test that forgets a provider stub does not fail
loudly: it makes a real request, retries it five times with backoff, and asserts against
whatever the live model answered. `test_review.py` did exactly that — twenty HTTPS
connections to a live endpoint from a test named `without_network` — and passed or failed
depending on the day. AGENTS.md rule 7 says tests stub the provider; the fixture enforces
it instead of trusting it.

The seam that was missed is worth naming, because it is the one a reviewer would not
expect: `review.revise` prefers the bounded repair loop, and that loop calls
`llm.call_tools` (native tools), **not** `llm.call`. Stubbing `llm.call` alone leaves the
loop talking to a real endpoint. `test_agent_loop.py` already handled this correctly by
setting `agent_loop.NATIVE_TOOLS = False` and stubbing `llm.call`; the review and
goal-wiring tests now stub both seams explicitly.

Loopback stays open on purpose: the browser tier drives headless Chrome over CDP on
`127.0.0.1`, and the preview and cloud-runner suites talk to a local server. Blocking that
would break them for no safety gain. `HARNESS_LIVE_FREE=1` opts the one intentional live
test back in.
