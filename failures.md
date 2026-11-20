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
