# Failures

One row per real run that stopped short or could not be called done. Test and eval runs
are redirected to a temp directory by `tests/conftest.py`; only runs a person started
belong here. The component column is the part of the harness that owned the failure.

| date | run | component | first failure | files |
| --- | --- | --- | --- | --- |
| 2026-11-20 | `7b94c105e449` + 6 more | provider | Ollama's MLX runner answered 501 `structured output is unavailable` when the planner asked for JSON mode; the run died instead of asking once without the optional field | `modules/adapters.py` |
| 2026-11-20 | `ebc1805d7ede` | provider | `ReadError` mid-stream ended the build after research; a transport drop is weather, not a bad credential | `modules/retry.py` |
| 2026-11-20 | `2ddc895f254f` | wiring | an unconfigured OpenAI specialist reached the run and answered 401 "no API key"; the workshop let a dead card launch | `src/lib/providers.ts` |
| 2026-11-20 | manual | provider | provider keys disappeared whenever the roster was replaced or reset because a key was tied to a browser-generated row id | `modules/roster.py` |
| 2026-11-20 | workshop | wiring | a fresh browser merged the four starter drafts into the saved roster on every load, so 3 specialists became 21 and the next save published the junk | `src/pages/Workshop.tsx` |
| 2026-11-20 | test suite | unknown | pytest runs appended their fake failures (`budget-stop`, `deadline`, `gate`) to this file, hiding the real ones | `tests/conftest.py` |
| 2026-11-20 | `761b7c2bb692` | plan | package.json: package.json was declared by the plan but is still empty | package.json, src/App.tsx, src/index.css, src/main.tsx |
| 2026-11-20 | `bb3ba11a24e3` | plan | package.json declared Next.js for a Vite tree; the planner owned a manifest it could not keep consistent | package.json |
| 2026-11-20 | `164f513cdb0d` | wiring | the preview was blank after a clean `vite build`: absolute `/assets/...` URLs 404 under the per-run token prefix | `vite.config.ts` |
| 2026-11-20 | `164f513cdb0d` | wiring | the reviewer found truncated JSX that the structural verifier called clean; the tool loop spent 8 steps and the standard rewrite fixed it | `nodes/verify.py` |
| 2026-11-20 | `eca0542ddaf1` | unknown | ValueError: '/private/tmp/cortex-quality-run/runs/eca0542ddaf1/src/main.tsx' is not in the subpath of '/tmp/cortex-quality-run/runs/eca0542ddaf1' OR one path is relative and the ot | — |
| 2026-11-20 | `9cfd515c54ea` | cancelled | cancelled | — |
| 2026-11-20 | `201c46c9795f` | cancelled | cancelled | — |
| 2026-11-20 | `ffcd2d65ccf5` | cancelled | cancelled | — |
| 2026-11-20 | `f6f6ef606ce8` | cancelled | cancelled | — |
| 2026-11-20 | `e5a6bc692125` | cancelled | cancelled | — |
| 2026-11-20 | `9b071fa4e727` | cancelled | cancelled | — |
| 2026-11-20 | `22bd95b75e20` | cancelled | cancelled | — |
| 2026-11-20 | `56139ed8e879` | unknown | ValueError: too many values to unpack (expected 2) | — |
| 2026-11-22 | `ab759ace5938` | cancelled | cancelled | — |
| 2026-11-22 | `3edf0cc9f978` | code | src/index.css: src/index.css was declared by the plan but is still empty | index.html, src/App.tsx, src/components/About.tsx, src/components/Contact.tsx |
| 2026-11-22 | `bd9db460b09e` | unknown | stopped without a reported cause | — |
| 2026-11-22 | `136000d54a3a` | code | src/App.tsx: does not parse:          ╵ ~~ | src/App.tsx |
| 2026-11-22 | `3cc7c0634dab` | code | cancelled | todo.md |
| 2026-11-22 | `348fbf9324b2` | code | src/main.tsx: does not parse:          ╵ ~~ | src/components/Header.tsx, src/index.css, src/main.tsx |
| 2026-11-22 | `c8d35d310f55` | unknown | stopped without a reported cause | — |
| 2026-11-22 | `c6232e08a963` | code | src/App.tsx: src/App.tsx was declared by the plan but is still empty | index.html, src/App.tsx, src/components/About.css, src/components/About.tsx |

## What changed

- **One check per entry**: the 501 fallback is pinned by `tests/test_adapters.py`, the
  read-drop classification by `tests/test_harness.py`, the keyring by `tests/test_keys.py`.
- **Key storage is now per provider endpoint** (`kind|baseUrl`) in a `0600` keyring
  (`HARNESS_KEYS_FILE`). Replacing, resetting or duplicating a roster row no longer
  touches its credential; only an explicit `clearKey` does.
- **The workshop waits for hydration** before it writes a roster back, and no longer
  appends drafts twice. A default team is seeded only when the harness has none.
- **The critic is a different specialist** from the author, and the plan must survive an
  adversarial `REASON_CRITIC` pass before code is written.

## Still open

- Live verification is an HTTP probe, not a browser driving the UI; interaction bugs still
  need Playwright or a human.
- The landing and build room clip at phone width; the office is a fixed-size canvas.
