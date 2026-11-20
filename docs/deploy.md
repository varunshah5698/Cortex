# Deploy Cortex on Render

`render.yaml` is a Render Blueprint for one free web service. It builds the React UI,
installs the Python harness, and serves both from the same origin:

- `dist/` is served by `ai_harness/main.py` at `/`
- the API is at `/api`
- `/api/health` is the health check and needs no password

## 1. Push the repository

```bash
git push -u origin main
```

## 2. Create the Blueprint

1. Render Dashboard → **New** → **Blueprint**
2. Connect `github.com/varunshah5698/Cortex`
3. Apply `render.yaml`

Render asks for no password. The site's first screen is a server-backed sign-up/sign-in
form. Passwords are PBKDF2-HMAC-SHA256 hashed in `.harness-auth.db`; the session is an
HttpOnly cookie, and `/api` on the public origin requires it. A script can still use
`X-Harness-Token`, and a loopback browser stays open for local development. The API
also rejects foreign browser origins.

## 3. Add provider keys

Either:

- **Render Dashboard → Environment** and set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `GROQ_API_KEY`, `OPENROUTER_API_KEY`, or `HARNESS_PROVIDER_KEY`; or
- open the deployed site → **Workshop** and save a key per specialist.

A key saved in the workshop lives in a `0600` file on the instance. On a free instance
that file is ephemeral, so environment variables are the durable option. The browser
never receives a saved key; `GET /api/roster` reports only `has_key`.

Accounts live in `.harness-auth.db` (migrated once from the old `workspace/auth.db`).
A free Render instance has an ephemeral disk, so accounts and sessions reset on deploy
or restart, and the login screen shows that warning. To keep them, upgrade the service,
attach a disk at `/var/cortex-data`, and set `HARNESS_AUTH_DB=/var/cortex-data/auth.db`
(the commented block in `render.yaml` is the exact shape). The session cookie is valid
for 30 days on a browser that keeps it. Keep the provider keys scoped and the per-run
caps low (`HARNESS_MAX_USD`, `HARNESS_MAX_TOKENS`); the service allows one run at a time
and two queued.

## 4. Open the site

Render gives you `https://<service>.onrender.com`. Create the first account on that
screen and you land in the workshop. The same origin is added to `HARNESS_ORIGINS`
automatically from `RENDER_EXTERNAL_URL`, so the signed-in UI can call `/api`.

## What is different from a local run

- **Previews run on the cloud runner.** `Run` uploads a finished workspace to the
  `cortex-runner` service, which serves it at a per-run capability URL; the Preview tab
  embeds that URL. Source previews while a run is still writing are still only on the
  local loopback origin.
- **Browser checks are off.** There is no Chrome on the server; `render.yaml` sets
  `HARNESS_BROWSER_CHECK=0`. The button reports `skipped`, not a false pass.
- **Runs are ephemeral.** `HARNESS_WORKSPACE=/var/tmp/cortex-workspace` survives while
  the instance is warm, not across deploys or free-tier sleeps. Attach a Render disk
  (paid plans) and point `HARNESS_WORKSPACE` at its mount to keep run history.
- **The sandbox is macOS-only.** On Linux the runner reports that processes are
  unsandboxed. Approvals (`HARNESS_REQUIRE_APPROVAL=1`) still gate installs and builds.

### Preview service

`render.yaml` creates two services: `cortex` (the harness and UI) and `cortex-runner`
(the public preview host). The harness uses the runner only when both are set:

```text
HARNESS_RUNNER_MODE=cloud
HARNESS_CLOUD_RUNNER_URL=https://<your-runner>.onrender.com
HARNESS_CLOUD_RUNNER_TOKEN=<same value as HARNESS_RUNNER_TOKEN on cortex-runner>
```

The Blueprint fills `HARNESS_CLOUD_RUNNER_URL` from the runner service host. Set the
same token in both services. A finished run then publishes to a URL like
`https://<runner>.onrender.com/runs/<id>/<secret>/`; the Preview tab embeds it and the
**Run** button is the only way to start it. The runner holds one run at a time on the
free plan and expires it after `HARNESS_RUNNER_TTL` seconds.

## Updating

Push to `main`; Render redeploys on every push (`autoDeploy: true`).
