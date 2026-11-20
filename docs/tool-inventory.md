# Tool inventory

One row per capability the harness or its agents can actually use. A tool whose
justification cannot be written down should be removed; a blank control column is an
incident waiting to happen. The enforced policy lives in `src/ai_harness/modules/permissions.py`
and is reported by `GET /api/health`.

| Tool or action | Why the agent needs it | Worst case, and the control |
| --- | --- | --- |
| `tools/file_manager.py` — scaffold and write a file in the run | Shipping the artifact is the point of the run | Overwriting outside the run's sandbox. Control: every path resolves through `utils/validators.safe_relative`; refusals are `error` events; byte and line quotas in `config/settings.py` |
| `tools/web_search.py` — search, then fetch one page | Grounding the build in something real when the request names it | Fetching an internal address (SSRF) or looping on the network. Control: `utils/netguard.py` refuses link-local/metadata hosts; at most 3 queries and 2 pages per run |
| `tools/terminal.py` — read-only inspection | Let a specialist look at the tree instead of guessing at it | Shell injection, credential reads. Control: off unless `HARNESS_ALLOW_TERMINAL=1`; program allowlist (`ls`, `cat`, `grep`, …); metacharacters refused; `permissions.decision` deny rules (`cat .env` is denied by path); cwd is the run workspace |
| `modules/runner.py` — `npm install` | The plan declares a `package.json`; without installs the build cannot run | Install scripts from the model's manifest execute on this machine. Control: **approval checkpoint** — the whole pipeline is planned first (`Service.plan()`) and an `ask` command blocks the start in `needs_approval` until a request carries `approve=1`; `permissions.safe_env()` strips provider keys and `HARNESS_*`; install timeout bounds it |
| `modules/runner.py` — `npm run build` | A React/Vite run is meaningless unless it compiles | The build script is arbitrary Node code. Control: same approval checkpoint (planned and listed before anything runs); deny rules for `curl`/`wget`/`git push`/`npm publish`; build timeout |
| `modules/runner.py` — `npm run dev` | Some stacks only exist under their dev server | Same as build, plus a long-lived process. Control: free loopback port only, `BROWSER=none`, `CI=1`, approval required, killed with its process group on stop |
| `modules/runner.py` — `pip install -r requirements.txt` | A Python run declares dependencies | Setup code from the model's requirements runs. Control: approval checkpoint; `pip install git+…` denied outright; install timeout |
| `modules/runner.py` — `uvicorn app:app` | Serving the Python app is how it gets verified | Executes model-authored Python as a server. Control: approval checkpoint; bound to `127.0.0.1` on a free port; the port is never advertised — the UI reaches it only through the preview proxy's per-run token |
| `modules/static_server.py` — serve a built tree | A static build needs a real origin for absolute `/assets/…` URLs | Serving a path outside the tree, or a local page reading it by scanning ports (a navigation carries no header to check). Control: `permissions.py` marks it `allow` (harness-owned, read-only); path containment in the server; loopback only; served only under the run's nonce (`--prefix`), every other path a 404 |
| `preview.py` — `GET /preview/{run}/{path}` on its own port | Looking at generated files without running them | Path traversal, or preview code reading `/api`. Control: separate origin with **no `/api` at all**, so a run's script can only reach the API cross-origin and the API refuses a foreign `Origin`; `safe_relative` containment; `permissions.DENY_PATHS` refused; `nosniff` + `no-store` |
| `preview.py` — `GET /live/{run}/{token}/…` proxy | The served app is arbitrary code and cannot be given auth, so it must not be reachable by port alone | Anyone on the machine hitting the model's server directly, or a leaked URL. Control: per-run secret (`Service.share`, `token_urlsafe(18)`, compared with `compare_digest`); the port is never sent to the browser; hop-by-hop headers stripped |
| `modules/toolchain.py` — the artifact's own `tsc --noEmit` | A type error is invisible to a structural check; only the compiler knows | Executing a model-installed compiler. Control: runs only when `node_modules/typescript` already exists (so an install was approved), `HARNESS_TYPECHECK=0` disables it, 120s timeout, stripped environment, `.py` files are compiled in-process and never imported |
| `modules/sandbox.py` — contain every child process | Approving `npm install` approves whatever an install script does | A dependency writing into the owner's home or reading a key. Control: macOS `sandbox-exec` profile — writes denied under `$HOME` except the run workspace and caches, `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc`, `~/.config/gh` unreadable; harness says in the log when no sandbox is available instead of implying one |
| `modules/mcp.py` — one allow-listed, read-only MCP server | The team needs one real system, not ten imagined ones | A remote caller gaining write access or the build through a server. Control: stdio only; a tool is callable only when it advertises `readOnlyHint` (or a read verb) **and** is in the server's `allow` list; starting the server needs `"allow_exec": true` in `harness.mcp.json`; 20s timeout; output clipped to 4000 chars |
| `POST /api/run/{id}/preview-check` — probe the served app | "It loads and the button does nothing" is the failure no file check can see | Probing an arbitrary URL. Control: only the runner's own loopback service URL, assets limited to the page's own host, no redirect to a foreign origin, GET only |
| `POST /api/run/{id}/resume` — continue from disk | A run that dies after building should not pay for the build twice | Re-running a run under its old id with different inputs. Control: the plan and files come from the run's own workspace, the terminal event is dropped first, and a run with no `.harness-plan.json` refuses instead of re-planning |
| `modules/llm.py` — provider calls | The models are the workers | Unbounded spend, or hammering a provider. Control: budget checked before each request (tokens, dollars, calls); per-model prices; retry that never retries auth failures; cache only at temperature 0; an optional single fallback model (`HARNESS_FALLBACK_MODEL`) so a hard outage does not end the run |
| `modules/roster.py` — provider keys | A key must never live in the browser | Reading keys back out. Control: written once to a `0600` file; `/api/roster` reports `has_key`, never the value; `/api` refuses foreign origins and requires `x-harness-token` |
| `GET /api/run/{id}/events` — SSE stream | Watching the room is the product | Streaming a run to a foreign page. Control: origin check plus token; loopback only |
| `POST /api/run/{id}/serve` — the approval itself | Somebody has to decide, and the decision should be informed | Approving blind. Control: returns `needs_approval` with the full command list; only `approve=1` proceeds; `HARNESS_REQUIRE_APPROVAL=0` exists for unattended CI and is off by default |

## Policy

`modules/permissions.py` holds an ordered rule table — deny, then ask, then allow — and a
command that matches no rule is **denied**, so a new capability is a deliberate rule rather
than an assumption. `denied_path()` refuses credentials and the harness's own trust store
(`.env`, `.harness-token`, `.harness-roster.json`, `.harness-keys.json`, keys, `secrets/*`) even inside an
allowed command. The decision for a whole pipeline is made before the first spawn:
`Service.plan()` lists what would run, `Service.start()` stops at `needs_approval`, and
`_spawn()` re-checks every command as defence in depth.
