# Per-run cloud runner

The harness can execute a finished run on a **separate Render service** instead of this
machine. One service hosts the harness; another hosts generated apps. The browser only
ever receives a per-run capability URL, never the runner token.

```
browser ── /api ──> cortex (harness, Basic auth)
   │                   │  POST tar.gz + X-Runner-Token
   │                   ▼
   └── iframe ──> cortex-runner /runs/<id>/<secret>/… ──> generated app (loopback)
```

## Run it locally

Two processes:

```bash
# runner
HARNESS_RUNNER_TOKEN=dev-token \
HARNESS_RUNNER_WORKSPACE=/tmp/cortex-runner \
HARNESS_RUNNER_PUBLIC_URL=http://127.0.0.1:8099 \
PYTHONPATH=src ./.venv/bin/python -m uvicorn ai_harness.cloud_runner:app \
  --host 127.0.0.1 --port 8099

# harness
HARNESS_RUNNER_MODE=cloud \
HARNESS_CLOUD_RUNNER_URL=http://127.0.0.1:8099 \
HARNESS_CLOUD_RUNNER_TOKEN=dev-token \
PYTHONPATH=src ./.venv/bin/python -m uvicorn ai_harness.main:app \
  --host 127.0.0.1 --port 8000
```

The Run button now submits the run to the runner. The preview iframe uses the returned
URL directly. `GET /api/health` still describes the harness; the runner has its own
`/health`.

## Deploy on Render

`render.yaml` defines both services. On a Blueprint sync, Render asks for:

| Service | Secret | Value |
| --- | --- | --- |
| `cortex` | `HARNESS_CLOUD_RUNNER_URL` | `https://cortex-runner-xxxx.onrender.com` |
| `cortex` | `HARNESS_CLOUD_RUNNER_TOKEN` | the same random token |
| `cortex-runner` | `HARNESS_RUNNER_TOKEN` | that random token |

Generate the token once, for example:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

The harness stores it server-side only. The runner accepts `POST /runs` only with
`X-Runner-Token`; the public proxy path carries a different per-run secret.

## Vercel mode (static and Node)

`HARNESS_RUNNER_MODE=vercel` deploys each finished static/Node run to a temporary Vercel
project (`cortex-<run_id>`) and uses the production deployment URL directly. The CLI
login on this machine is enough locally; a headless host sets `HARNESS_VERCEL_TOKEN`.
A run is removed at `HARNESS_VERCEL_TTL` or on `POST /serve/stop`.

The project is created with an exact name, SSO protection is disabled (it is a public
per-run preview by design), and the whole project is removed at teardown. A Python ASGI
tree is not sent to a static host: it falls back to the local runner and the UI shows the
local service state.

On Render, the build command installs the CLI into `.vercel-cli/` because the runtime
cannot write `/usr/lib/node_modules`; `modules/vercel_runner.py` finds that local binary.

## Limits and safety

- `HARNESS_RUNNER_TTL` (default 900s) stops and deletes a run.
- `HARNESS_RUNNER_MAX_RUNS` (default 1) keeps a free 512 MB instance honest.
- The archive is capped at 25 MB and 2,000 entries; links, devices and `..` paths are
  refused during extraction.
- The runner has no `/api`, roster, provider keys or `.env`; its child processes get
  `permissions.safe_env`.
- Generated code runs as a process on the runner. On Linux the runner reports that the
  macOS sandbox is unavailable; approvals still gate installs/builds. Use a dedicated
  runner instance, not a machine that holds credentials.
- A React/Next install may exceed a free instance's memory. The runner reports the
  install/build failure with its own log; it does not pretend the run is ready.

## Stop

`POST /api/run/<id>/serve/stop` deletes the run on the runner. Harness shutdown calls
`DELETE` for every remote run it knows. A runner restart or TTL expiry also removes the
workspace.
