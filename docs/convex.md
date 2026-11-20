# Convex: the durable data layer

Cortex has two systems, and the split is deliberate.

- **The Python harness executes.** It plans, writes files, verifies, and decides `done`.
  It owns the workspace and the run's truth. Nothing here changes that.
- **Convex stores what has to outlive a process.** Accounts, sessions, threads, runs and
  their events, the specialist roster, and the lessons a failed run taught.

Before this, threads lived in `localStorage` (gone when you changed browser) and accounts
lived in a SQLite file on an ephemeral disk (gone when Render restarted). A run's history
was a JSONL file on one machine. Convex is the fix for exactly that, and nothing more.

## Deployment

| | |
| --- | --- |
| Cloud URL | `https://dynamic-jay-407.convex.cloud` |
| HTTP Actions | `https://dynamic-jay-407.convex.site` |
| Dashboard | `https://dashboard.convex.dev/t/varunshah0010/cortex/dynamic-jay-407` |

Deploy with the deploy key from `.env` (never committed):

```bash
CONVEX_DEPLOY_KEY='dev:dynamic-jay-407|…' npx convex deploy --yes
```

## Tables

| Table | Holds | Why it is not in the workspace |
| --- | --- | --- |
| `users` | email, PBKDF2 hash + per-user salt | a restart must not delete a login |
| `sessions` | hashed session token, expiry | a leaked read cannot be replayed |
| `threads` / `messages` | conversations | history follows the person, not the browser |
| `runs` | one row per harness run, keyed by the harness's own `runId` | a run is readable after the process is gone |
| `events` | append-only, monotonic `seq` per run | resume a stream instead of replaying it |
| `specialists` | role, provider, model | shared roster; **no key field exists** |
| `lessons` | a verification failure that became an instruction | a lesson outlives the machine |

## Two write paths, on purpose

1. **Browser → Convex functions** (`convex/threads.ts`, `runs.ts`, `roster.ts`, `auth.ts`).
   Every function resolves the session token to a user first, then scopes the query to
   that user. A thread id is not a capability: someone else's id returns empty.
2. **Harness → `.convex.site` HTTP actions** (`convex/http.ts`). The Python process has no
   Convex client and no session, so it presents `CONVEX_HARNESS_SECRET` as a bearer token.
   The secret is compared before anything is read or written, and **an unset secret is a
   refusal, not an open door**. The `*BySecret` mutations re-check it, because a check that
   lives only in the caller is not a check.

## The mirror never breaks the run

`modules/cloud_sync.py` is best-effort and time-bounded (`CONVEX_SYNC_TIMEOUT`, default
4s). A Convex outage, a wrong secret or a slow network returns `None` and the build
continues, because the build already succeeded on disk. `enabled()` requires **both** the
site URL and the secret, so an unconfigured checkout behaves exactly as it did before.

`/api/health` reports `cloud: {enabled, site, secret_set}` — configuredness, never the
secret. A health endpoint is public.

## Environment

Local (`.env`, gitignored, mode 600):

```
CONVEX_DEPLOYMENT=dev:dynamic-jay-407
CONVEX_URL=https://dynamic-jay-407.convex.cloud
CONVEX_SITE_URL=https://dynamic-jay-407.convex.site
CONVEX_DEPLOY_KEY=dev:dynamic-jay-407|…
CONVEX_HARNESS_SECRET=<64 hex chars>
VITE_CONVEX_URL=https://dynamic-jay-407.convex.cloud
VITE_CONVEX_SITE_URL=https://dynamic-jay-407.convex.site
```

Only `VITE_`-prefixed values reach the browser bundle. The deploy key and the harness
secret have no `VITE_` prefix and must never get one.

On Render, set `CONVEX_SITE_URL` and `CONVEX_HARNESS_SECRET` as service env vars
(`sync: false` in `render.yaml` so they are not in the repository).

## Known gaps

- The mirror records a run's **start, verify and terminal verdict**, not every token. A
  full event replay would need the orchestrator to forward each `emit`, which is a
  per-token network call and belongs behind a queue, not in the hot path.
- There is no migration of existing `localStorage` threads or the old SQLite accounts. A
  person signed in before this change has to sign up once more.
- `runs` and `events` are keyed by the harness `runId` but not by user in the harness path:
  the secret path has no user, so those rows are written with no `userId`. Reading them
  requires a session that owns the run, which today means runs started from the browser.
