# Architecture

## The loop

```
prompt
  └─ planning      planner splits the work, scaffolds every file empty, assigns owners
      └─ research  (skipped on Quick) web search + page text → notes for every writer
          └─ contracts  each specialist publishes the interface of its file
              └─ building   all specialists reason, then write, concurrently
                  └─ review findings → Q&A → rewrites
                      └─ synthesizing  one agent reports what shipped
```

Each node is a function over `RunState` in `core/state.py`; `core/orchestrator.py` runs
them in order; `core/harness.py` owns the registry of live runs.

## Why it is structured this way

A harness is the scaffolding around a model, not the model. This one supplies:

- **context** (`modules/context.py`) — the request, peer contracts, research notes,
  clipped so a small local model can still hold it.
- **tools** (`tools/`) — a file manager sandboxed to the run workspace, a DuckDuckGo
  search/fetch pair, and an allowlisted terminal that stays off unless `HARNESS_ALLOW_TERMINAL=1`.
- **memory** (`modules/memory.py`) — run memory, contracts and notes, persisted with the run.
- **sensors** (`nodes/review.py`) — a reviewer that reads the produced files instead of
  trusting the authors. Sensors run after guides: the review findings become the next prompt.
- **guardrails** (`utils/validators.py`) — path escapes, oversized files and empty
  responses are rejected at the tool boundary, not in the prompt.

## Scaffolding is stack-aware

The plan decides the file tree, and the plan is allowed to pick any language. A React
request scaffolds `package.json` + `src/main.tsx` + `src/App.tsx` + `src/index.css`, a
Python request scaffolds `app.py` + `requirements.txt` + `test_app.py`, and the plain
`index.html`/`styles.css`/`app.js` trio is only the fallback used when the planner's
JSON cannot be parsed. `agents/planner.py::infer_stack` holds those families.

Scaffolding happens before any model writes: `FileManager.scaffold` creates the empty
files, nested directories included. During the building phase the executor may emit
`<<<FILE path>>>` to start a file the plan did not name, so the tree can grow into
another language as the work reveals the need — the verifier, file explorer and
preview all read the workspace rather than a fixed manifest.

## Runtime guarantees

These are the concerns that are cheap to add and expensive to add later, so they live
in the harness rather than in any prompt. All of them except the cache are on by
default, and all are configurable through the environment (see `.env.example`).

| concern | module | what it actually does |
| --- | --- | --- |
| money | `modules/budget.py` | prices token usage into `cost_usd` (USD / 1M tokens, longest model-name match, local endpoints free) and holds the cap — tokens, dollars, or provider calls — checked **before** each request and settled after it. `BudgetExceeded` is never swallowed by a node's error handling. A model with no row is still charged at the default — a run must stay bounded — but never silently: `row_for` names the row `"default"`, `GET /api/health` lists it under `pricing.unpriced`, and the live eval suite refuses to start until it has one |
| time | `modules/runtime.py` | `HARNESS_MAX_SECONDS` is checked beside the budget at every provider call (`llm.stream` and `llm.call`, the latter before the cache hit). A run past it may not start another call; `core/orchestrator.py` ends it with a `timed_out` error that lists the files on disk and never with `done` |
| cache | `modules/cache.py` | **opt-in and determinism-gated**: `HARNESS_CACHE=1` *and* `HARNESS_TEMPERATURE=0`, because at any temperature above zero the provider samples and a replay would serve a different artifact. LRU keyed by provider + model + temperature + messages, used only by non-streaming `llm.call`; a replay reports `cached: 1` and costs nothing |
| retry | `modules/retry.py` | exponential backoff for 429/5xx/timeouts, immediate re-raise for 401/403/404/400; never retries a `BudgetExceeded` and never retries a stream mid-file |
| provenance | `modules/prompts.py` | every prompt constant is content-hashed; `metrics.prompts` records the exact wording a run used, so a wording change is traceable |
| secrets | `modules/roster.py` | specialists and their keys live server-side in a `0600` file; `/api/roster` returns `has_key`, never the key, and a run is handed the stored key by id so the browser never posts one |
| verification | `nodes/verify.py` | deterministic checks after building and after revision, chosen by file type so every stack gets a real sensor: empty declared files, placeholder text, unbalanced braces; markup linking an asset nobody wrote or a script wiring an id no element declares; `*.json` that does not parse (comments tolerated for tsconfig); relative imports that resolve to no written file in JS/TS/Vue/Svelte, Python, Go (under the `go.mod` module) and Rust (`mod x;`) |

Temperature is a setting (`HARNESS_TEMPERATURE`, default `0.35`), not a constant buried
in the client, and it is part of the cache key. `llm.cacheable()` is the single gate:
caching requires the flag **and** `TEMPERATURE == 0`. A sampled run is never replayed.

`modules/runtime.py` binds the current run and its budget into context variables, so
`llm.py` can enforce a budget and report cache hits or retries without every call
site growing a parameter. `asyncio.create_task` copies that context, so concurrent
specialists in one run share exactly that run's budget.

### The reviewer can be overruled

The verification node runs before the model review and its findings are appended to
the reviewer's system prompt as evidence. If the reviewer still answers `APPROVED`
while the deterministic checks found errors, the harness discards the approval and
feeds the facts into the revision loop instead. A model's opinion never outranks a
check that read the files.

## Two passes per file

`agents/executor.py` never writes code straight out of the prompt:

1. `reason()` — a deliberation ladder (restate → questions → options → decision → risks →
   blueprint → self-check) streamed to the UI as `think` events.
2. `compose()` — the file, written against the agent's own deliberation. The model may
   emit `<<<FILE path>>>` markers to start another file.

## HTTP surface

`main.py` guards every `/api` route before the handler runs. Artifacts are not served by
`main.py` at all: `preview.py` is a second app on its own loopback port
(`HARNESS_PREVIEW_PORT`, default 8001) that exposes `/preview/<run>/…` and a `/live/…`
proxy, and **no `/api`**. The `/live/…` proxy also forwards to a static tree's
`/<nonce>/…` path: that server is reached by a browser navigation, which carries no
header a check could use, so the run's nonce lives in the path and a bare port scan 404s. Two origins are the point — model-written code in a preview
cannot reach the API with a same-origin allowance, and the UI frames it sandboxed without
`allow-same-origin`, so it also has no storage and no way to navigate the top window. A request carrying
`Origin` must match `HARNESS_ORIGINS` or the process's own host; a request with no
browser provenance (`Origin` and `Sec-Fetch-Site` both absent — curl, a script) must
present `X-Harness-Token`. `Sec-Fetch-Site` is a forbidden header, so a page cannot
forge it into looking same-origin — but a rebound name resolves to `127.0.0.1`, which
makes `Origin`, `Host` and `Sec-Fetch-Site` all agree, so both same-origin shortcuts
additionally require the request's host to be a literal loopback name, and compare
parsed `(host, port)` pairs rather than netloc strings — `urlparse` reports the host of
`127.0.0.1:8000.evil.example` as `127.0.0.1` and only rejects the port when it is read. `GET /api/health` stays open so a UI can report an
offline harness. CORS is restricted to the same origin list: a wildcard let any page on
the web drive the harness through the user's tab, spending whatever keys it held.

Paths are resolved, never string-matched. `_run_dir` accepts one `[A-Za-z0-9_-]`
segment under `RUNS_ROOT`, and file reads go through `utils/validators.safe_relative`,
which resolves the real path. A `startswith` check admitted `runs/abc-sibling` whenever
the workspace was `runs/abc`; the verifier's own escape tests never covered the HTTP
surface, so this is now checked by an endpoint test as well.

## Event contract

Server-sent events on `GET /api/run/{id}/events`:

| event | payload |
| --- | --- |
| `start` | `prompt`, `resumed` (true for a run continued from disk) |
| `phase` | `planning`, `scaffolding`, `research`, `contracts`, `building`, `reviewing`, `revising`, `synthesizing` |
| `status` | `agent`, `idle/thinking/writing/researching/reviewing/talking/done/error` |
| `plan` | `objective`, `units[]` |
| `scaffold` | `files[]` created empty before any model call |
| `research` | `query`, `title`, `url`, `snippet` |
| `usage` | `agent`, `phase`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `ms`, `calls` |
| `message` | `from`, `to`, `text`, `kind` |
| `think` | `agent`, `delta` |
| `tool` | `tool`, `args`, `result`, `ms`, `ok`, `agent`, `phase` (and `error` when it failed) — one per tool call: file manager, web search, MCP, and every install/build/serve the runner starts |
| `file_start` / `file` / `file_end` | `path`, `delta`, `lines` |
| `verify` | `stage`, `ok`, `errors`, `warnings`, `checked`, `issues[]`, `toolchain` (`checked`, `note`) |
| `thrash` | `tool` or `path`, `count`, `message` — an identical call repeated past the soft cap |
| `fallback` | `agent`, `from`, `to`, `message` — retries exhausted, continued on the fallback model |
| `retry` | `agent`, `attempt`, `delay_ms`, `message` |
| `budget` | `message`, `tokens`, `usd`, `max_tokens`, `max_usd` |
| `done` | `files[]`, `metrics`, `build` (`null` until a preview compiled the run), `previewed` |
| `error` | `message` |

`usage` events also carry `cost_usd` and `cached`, and `metrics` carries `budget`,
`cache`, `verification`, `build`, `previewed` and `prompts` alongside the token ledger.

`verify` runs in tiers. Tier 1 is structural (`nodes/verify.py::check`: contradictions
between files, per stack). Tier 2 is the artifact's own toolchain (`modules/toolchain.py`:
Python compiled in-process, the project's `tsc --noEmit` when a run installed TypeScript).
Tier 3 is `POST /api/run/{id}/preview-check`, which probes the live service and the assets
its HTML names; it emits a `verify` event with `stage: "preview"`.

Processes the runner starts are contained by `modules/sandbox.py`: on macOS every child is
wrapped in `sandbox-exec` with writes denied under the owner's home (except the run
workspace and package caches) and key stores unreadable. Where no sandbox exists the
service log says so — `GET /api/run/{id}/serve` reports `sandbox` in its own words.
Cache hits are reported as a `message` with `kind: "cache"` so they never inflate the
token ledger.

`done` is a verification verdict, not a build report: the pipeline checks the files and
never installs or compiles them, so `done` carries `previewed: false` and `build: null`
until `POST /api/run/{id}/serve` actually runs. Serving writes the service's state into
the run and `.harness-metrics.json` (`previewed: true`, `build: {state, kind, detail, at}`),
and a restored run reads it back from the ledger, so a reload cannot make a served run
claim it was never compiled. `GET /api/run/{id}/serve` refreshes that record as the
install/build settles.

The UI in `src/lib/harness.ts` types exactly this shape.

## Serving

`modules/runner.py` keeps one `Service` per run. `POST /api/run/<id>/serve` starts the
pipeline in the background and returns at once; `GET /api/run/<id>/serve` reports the
state plus the log tail, and the UI polls while the service is busy. Every service —
`python -m http.server` for a static build or a plain `index.html`, `npm run dev` for a
dev server, `uvicorn` for an ASGI app — gets a free loopback port, and the UI points the
preview straight at it. A `<<<FILE path>>>` section therefore becomes a file that can
actually be built, not just a file that exists.

## Storage

`workspace/runs/<run_id>/` is the sandbox. `.harness-metrics.json` holds the usage
ledger, so `#/metrics` can read old runs after the server restarts.

## Runs are queued, not just started

`core/harness.py` caps how many runs execute at once (`HARNESS_MAX_CONCURRENT_RUNS`, 2 by
default) and how many may wait for a slot (`HARNESS_MAX_QUEUED_RUNS`, 8). A run that has
to wait emits a `queue` event with its position before it does anything else, so the UI can
say "waiting" instead of looking hung; a run cancelled while still queued records that as
an error event and gives its place back. Beyond the queue limit the API answers `429` with
`Retry-After` rather than accepting unbounded work it cannot run. `/api/health` reports
`running`, `queued` and `concurrency`.

## Evals

`python -m ai_harness.evals` runs five golden cases through the real pipeline — planning,
scaffolding, execution, verification, review — with the model replaced by a deterministic
script (`evals/script.py`). No key, no network, no spend, and the same result twice. Two
cases assert that a broken build is *reported* broken, so the suite checks the sensors as
well as the writers.

Each case is scored on facts: did it finish, did the verifier reach the expected verdict,
are the files that should have content non-empty, and did a passing run stay clean. The
result JSON records prompt fingerprints, so a score change can be read against the wording
that produced it. `--live` sends the same prompts to the configured roster (and spends
credits), `--update-baseline` records the current scores, and `--compare` exits non-zero
when a case drops below them.

`--build` adds a second sensor over the same tree (`evals/build.py`). The verifier is
static: it reads the files and reports contradictions, so a tree that does not compile
can still score clean. Every case whose tree is meant to run (`servable`) has its files
written into a scratch workspace and handed to `modules/runner.py` — the code the
Preview button uses, with approval — and the case fails, exit status included, unless
the service reaches `ready` *and*, when the tree declares a `build` script, what answers
is the built output rather than `npm run dev` (the runner's fallback when a build fails).
It installs and compiles, so it is opt-in and never part of the offline suite.

`--judge` (with `--live`, which it requires because it sends the request to a real model)
adds the rubric tier over the same run: the request, every file cut to 2,000 characters
and the verifier's findings go to the reviewer at `temperature=0`, and it answers two
0-10 marks — does this address the request, do the files agree with each other — whose
lower one becomes the score. The wording is `prompts.JUDGE`, so it is content-hashed
like every other prompt. The score is printed and saved and **never enters the exit
status**: a sampled grader must not be able to fail a build, so the deterministic checks
remain the only gate.
