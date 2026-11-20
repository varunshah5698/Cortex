# Cortex — a chain-of-command harness

Cortex puts a team of models in a room and lets them build a page together: a planner
splits the request, researches the web when it needs to, scaffolding creates the empty
file tree, every specialist deliberates out loud and then writes its own file at the
same time, and a reviewer reads the result and sends fixes back to the authors.

The UI is React (Vite). The harness itself is Python.

## Run it

```bash
bash scripts/setup.sh          # venv, ruff + pytest, npm

# harness on :8000, UI on :5178
PYTHONPATH=src ./.venv/bin/python -m uvicorn ai_harness.main:app --host 127.0.0.1 --port 8000
npm run dev

# every check in one command — lint, tests, types
bash scripts/verify.sh
```

`AGENTS.md` is the instruction file the coding agents read before they work: structure,
the verified commands above, the conventions and the things they must not do.

### Deploy

`render.yaml` deploys the UI and the harness as one Render web service, with a second
`cortex-runner` service for published previews. See `docs/deploy.md` for the Blueprint
steps and the features that stay local (source previews, browser checks). The site has
real sign-up/sign-in; accounts live in `HARNESS_AUTH_DB` and need a Render disk to
survive restarts.

Open `http://127.0.0.1:5178/#/build`. Configure specialists first in the workshop
(`#/setup`): each one is a provider, model and role. A key typed there is sent once to
the harness and stored on the server; the browser keeps only the non-secret fields.

## Where things live

```
src/ai_harness/          the Python harness (agents, nodes, tools, core)
src/pages/Build.tsx      the pixel office build room
src/pages/Files.tsx      file explorer with per-file code view
src/pages/Metrics.tsx    latency, token usage and cost
workspace/runs/<id>/     the sandbox for one run
```

The plan picks the stack, not the harness: a React request scaffolds `.tsx` files and a
manifest, a Python request scaffolds `app.py` + `requirements.txt`, and the vanilla
`index.html`/`styles.css`/`app.js` trio is only the fallback when the planner's JSON
cannot be parsed. Files are created empty before any model writes. An agent may return
several files in one answer: every `<<<FILE path>>>` line starts a new section, and each
section is written to its own path, registered in the run's file list and read by the
verifier.

See `docs/architecture.md` for the pipeline and the event contract.

## Running what the team wrote

A finished run is not a screenshot. The preview starts a real service: `modules/runner.py`
detects the stack, installs what the manifest declares, builds it when the stack has a
build step, and serves the result.

- `package.json` → `npm install` → `npm run build`, served from `dist/`, `build/` or
  `out/` on a free loopback port; if the build leaves nothing servable it falls back to
  `npm run dev` on another free port.
- FastAPI/Starlette → `pip install -r requirements.txt` → `uvicorn app:app` on a free
  loopback port.
- plain `index.html` → served exactly as written, on its own loopback port.

Every service gets a port, so absolute asset URLs (`/assets/index.js`) resolve the way
the build intended instead of being rewritten by the harness.

The Preview tab reports the state (`detecting`, `installing`, `building`, `starting`,
`ready`, `failed`, `needs_approval`), the port and the server's own log, so a failed
install is shown with its output instead of a blank frame.

`needs_approval` is deliberate. An install, a build and a model-authored server are an
`ask` decision in `src/ai_harness/modules/permissions.py`, so Run stops before executing
any of it and the preview offers **Approve & run**; the child processes also get an
environment with no provider keys and no `HARNESS_*` settings (`permissions.safe_env`).
The whole rule table is at `GET /api/health`. Run/Stop control the process, and the harness stops
every service it started when it shuts down. Limits: `HARNESS_INSTALL_TIMEOUT`,
`HARNESS_BUILD_TIMEOUT`, `HARNESS_BOOT_TIMEOUT`.

## Guardrails

A revision is a bounded tool loop before it is a rewrite. The author reads the real
file, applies a targeted `edit`, runs the same deterministic checks the reviewer sees,
and repeats — up to `HARNESS_AGENT_STEPS`. `done` is refused while the current check
fails, and a model that cannot produce the one-JSON-object protocol falls back to the
old whole-file rewrite. The prompt is rebuilt from disk each turn (objective, findings,
action ledger, an LRU working set of files, last observation) so the context stays flat
instead of growing with the loop. Turn it off with `HARNESS_AGENT_LOOP=0`.

OpenAI-compatible providers can take the same seven tools as native tool calls
(`HARNESS_NATIVE_TOOLS`); a provider that rejects them falls back to the text protocol
for the rest of the loop.

After a preview answers, **headless Chrome opens it** (`modules/browser.py`,
`scripts/browser_check.mjs`, no npm dependency): console errors, page exceptions, failed
requests, an empty body, and whether up to two buttons change the page. The verdict is a
`verify` event with `stage: "browser"` and `run.browser` in the ledger. It is a post-serve
sensor, so it never turns a `done` run green after the fact. `HARNESS_BROWSER_CHECK=0`
disables it.

**Public share** publishes one ready run through a one-run origin (`share.py`) and a
temporary tunnel (`modules/cloud.py`, `cloudflared` by default). The app has the run id
baked in, a path token, and no `/api`; the first request is an approval prompt, and the
URL expires with `HARNESS_SHARE_TTL`. It never exposes the multi-run preview app.
The default provider is `cloudflared` (`brew install cloudflared`); set
`HARNESS_TUNNEL_COMMAND` with a `{url}` placeholder for another tunnel.

**Per-run cloud execution** is a second mode:
`HARNESS_RUNNER_MODE=vercel` deploys static/Node runs to a temporary Vercel project and
uses its production URL; `HARNESS_RUNNER_MODE=cloud` sends the finished workspace to a
separately deployed `ai_harness.cloud_runner` service instead. The harness never sends
its `/api`, roster or keys to either. Vercel deployments are removed at
`HARNESS_VERCEL_TTL`; the cloud runner is TTL-bounded too. See `docs/cloud-runner.md`;
`render.yaml` defines the harness service; the optional separate runner is documented in `docs/cloud-runner.md`.

A run is bounded and auditable, not just observable. Every call is priced from a
model table and charged to the run's ledger; a token or dollar budget stops the run
before the next request rather than after the damage; identical non-streaming
requests replay from an LRU cache only when you opt in with `HARNESS_CACHE=1` **and**
run at `HARNESS_TEMPERATURE=0` — above zero the provider samples, so a replay would
serve a different build; transient provider failures retry with backoff while auth
failures fail immediately. After the files are written a
deterministic verifier checks the tree, per stack — empty declared files, markup
linking an asset nobody wrote, a script wiring an id no element declares, a JSON
manifest that does not parse, relative imports that resolve to nothing in JS/TS,
Python, Go or Rust — and its findings are given to the reviewer as evidence it cannot
dismiss. A run also cannot be reported as finished on a failing check: after the
review's revision pass the orchestrator allows `HARNESS_REPAIR_ROUNDS` repair rounds and
then emits a terminal `error` instead of `done`.

### Keys and the API surface

The browser never holds a provider key. A key typed in the workshop goes once to
`PUT /api/roster`, is stored in a `0600` keyring (`HARNESS_KEYS_FILE`) keyed by provider
endpoint, is merged into the
specialist when a run starts, and is never returned — `GET /api/roster` reports
`has_key`. Keys written to `localStorage` by earlier builds are dropped on load. To keep
keys out of the browser entirely, set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GROQ_API_KEY`, `OPENROUTER_API_KEY` or `HARNESS_PROVIDER_KEY` in `.env`; a stored key
wins over these.

**Generated code never runs on the API's origin.** Previews and served runs are served by
a second app (`ai_harness/preview.py`) on its own loopback port — `HARNESS_PREVIEW_PORT`,
default 8001 — which has no `/api` at all, and the build page frames it with
`sandbox="allow-scripts …"` and no `allow-same-origin`. A script a model wrote therefore
has an opaque origin: it cannot read the roster or start a run, because that would be
cross-origin and the API refuses a foreign origin. A served run is reachable only through
`/live/<run>/<token>/…`, where the token is a per-run secret the port itself does not know.
A static tree is also served only under `/<token>/…` on its own port: headers cannot tell
a browser navigation apart, so the path carries the nonce and a bare port scan returns 404.
The cost: generated pages that rely on `localStorage` or relative `fetch()` will not work
inside the sandbox.

`/api` answers the UI and nothing else. A browser request must come from
`HARNESS_ORIGINS`, or be same-origin with a literal loopback host (a rebound DNS name
that points at 127.0.0.1 does not count), or be same-origin with the deployed
`HARNESS_PUBLIC_ORIGIN` / `RENDER_EXTERNAL_URL`; anything without browser provenance — curl, a script, another page — must present `X-Harness-Token`, from
`HARNESS_TOKEN` or the generated `.harness-token` (mode 0600). `GET /api/health` stays
open so a UI can report an offline harness. File reads resolve the real path inside
`runs/<id>` and refuse `..`, absolute paths and a sibling directory that merely shares a
string prefix; a run id that is not one plain segment is a 404.

Tune it in `.env` (see `.env.example`): `HARNESS_MAX_TOKENS`, `HARNESS_MAX_USD`,
`HARNESS_TEMPERATURE`, `HARNESS_CACHE`, `HARNESS_RETRY_ATTEMPTS`, `HARNESS_PRICES`. `GET /api/health`
reports the active limits and cache counters; run metrics record the budget, the
verification report and the prompt fingerprints that produced the run.

## Providers

Any OpenAI-compatible endpoint plus a local Ollama daemon. Only specialists with a
base URL and a model are sent to the harness; a local model needs no key.
