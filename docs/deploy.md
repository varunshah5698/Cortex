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

Render asks for `HARNESS_BASIC_AUTH` during the first sync. Enter a password, or
`user:password`. This is not optional in practice: without it, anyone with the URL can
start runs and spend the provider keys stored on the server. `/api/health` stays open.

## 3. Add provider keys

Either:

- **Render Dashboard → Environment** and set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `GROQ_API_KEY`, `OPENROUTER_API_KEY`, or `HARNESS_PROVIDER_KEY`; or
- open the deployed site → **Workshop** and save a key per specialist.

A key saved in the workshop lives in a `0600` file on the instance. On a free instance
that file is ephemeral, so environment variables are the durable option. The browser
never receives a saved key; `GET /api/roster` reports only `has_key`.

## 4. Open the site

Render gives you `https://<service>.onrender.com`. The browser prompts for the Basic
auth password once. The same origin is added to `HARNESS_ORIGINS` automatically from
`RENDER_EXTERNAL_URL`, so the UI can call `/api` without a token.

## What is different from a local run

- **Previews and sharing need loopback.** `preview.py` serves model-written code on a
  separate loopback port by design. A single Render service cannot expose that port, so
  the Preview tab and public share links do not work in this deployment. The build,
  review, files and metrics pages do.
- **Browser checks are off.** There is no Chrome on the server; `render.yaml` sets
  `HARNESS_BROWSER_CHECK=0`.
- **Runs are ephemeral.** `HARNESS_WORKSPACE=/var/tmp/cortex-workspace` survives while
  the instance is warm, not across deploys or free-tier sleeps. Attach a Render disk
  (paid plans) and point `HARNESS_WORKSPACE` at its mount to keep run history.
- **The sandbox is macOS-only.** On Linux the runner reports that processes are
  unsandboxed. Approvals (`HARNESS_REQUIRE_APPROVAL=1`) still gate installs and builds.

## Updating

Push to `main`; Render redeploys on every push (`autoDeploy: true`).
