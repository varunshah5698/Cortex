# Freeing the model

The request was "the model is very much constrained, free him, let me think freely." This
document records what was actually constraining it, which of those were deliberate
guardrails worth keeping, and what changed. Every number here is a setting in
`config/settings.py` or a literal in the code named beside it.

## The finding

The harness was not short of capability. It was short of *room*. Three separate classes of
constraint were stacked on top of each other, and each one made the next one worse:

1. **A hard output ceiling of 8,192 tokens on every provider call.** At roughly twelve
   tokens a line this makes a 900-line file physically unwritable in one answer. A model
   that planned a complete file was cut off mid-file, and the truncation read as a weak
   model rather than a small budget.
2. **Deliberation that was paid for and then thrown away.** The reasoning pass was allowed
   4,200 characters, and the code pass was handed a third of it: `context.clip` takes
   tokens, and the literal was `700`. The model thought, then wrote code against a
   fraction of its own thinking, on every file.
3. **Tools that arrived only after failure.** `modules/agent_loop.py` is a real tool loop —
   read, edit, check, done, with a check-gated finish — and it was reachable only from
   *repair*. The build itself was one-shot generation. The model could not look at the
   tree, could not check its own work, and could not fix anything until a reviewer had
   already failed it.

The third is the structural one. The first two are budgets.

## What was a real guardrail, and stays

Not every bound was a leash. These exist because something went wrong without them, and
they are unchanged:

- The **check gate** on `done`. A loop cannot report success on a failing tree.
- **`thrash`** and the **no-progress gate**. A loop that repeats itself stops.
- The **path sandbox** and the **frozen-test rule**. A run cannot edit its own judge.
- **Budget, deadline and stream bounds.** A run is still bounded in money, wall clock and
  per-call time. The 46-minute review that motivated `STREAM_SECONDS` is a real failure.
- **Separate critic.** The model that wrote a plan is still its worst judge.

The distinction used throughout: a bound that stops a *broken* process is a guardrail; a
bound that stops a *working* process is a leash. Only the second kind was raised.

## What changed

### Output ceiling: 8,192 → 32,768

`MAX_OUTPUT_TOKENS`. The single hardest bound in the harness. A modern model answers 32k
without strain, and one answer may carry several files behind `<<<FILE>>>` markers.

Both adapters now degrade instead of failing: an OpenAI-compatible provider that rejects
the value answers 400, the field is dropped, and the provider applies its own ceiling. The
same for Ollama's `num_predict`. A generous number must never be the reason a call fails.

### Truncation became actionable

`StreamStats.truncated` was set correctly and then thrown away. `executor.compose` now
carries it onto the `Usage` it already returns, and `execution.write_unit` acts on it: a
cut-off file gets one **continuation** pass (`prompts.CONTINUE_FILE`) that is given the
tail of what the model already wrote and asked for the rest.

This matters because the old behaviour was worse than it looks. A truncated file was
shipped, the verifier reported a syntax error, and the author was sent into a repair loop
over a file it had never finished writing — asked to fix code it never produced.

### Deliberation: 700 → 1,600 words

`REASONING_WORDS`. Seven hundred words across nine deliberation stages is about 78 words a
stage, which is a checklist, not thinking. The character cap moved with it (4,200 →
9,600).

The clips that carry the plan into the code pass are now derived from that budget rather
than literals: `REASONING_CLIP_TOKENS` and `CRITIQUE_CLIP_TOKENS`. Two hardcoded values
(`1100` for the critic, `1800` for the second critic) were overriding the knob that exists
for exactly this purpose, so raising the knob changed nothing. Both now use it.

### The repair loop: 8 → 24 steps, 2 → 4 active files, 12k → 32k context

`AGENT_STEPS`, `AGENT_ACTIVE_FILES`, `AGENT_CONTEXT_CHARS`, `AGENT_OBSERVATION_CHARS`.

Eight steps was sized for "one broken name." A real repair is read, find the second
occurrence, edit, check, fix the follow-on, check again, done — eight before anything goes
wrong. A loop that runs out of steps mid-fix hands a half-repaired file to the whole-file
rewrite, which is how the parts that already worked get lost.

The working set held two files. A repair that needs the markup, the script and the
stylesheet in view at once could not, and re-read them every turn instead.

### Per-file check at every depth, not only `ultra`

`execution.write_unit`. The freshly written file was checked and repaired *only* on
`ultra`. A default run wrote every file, moved on, and discovered the broken one at the
end-of-run review, by which point the author's context was gone.

It now runs at every depth, and the set of codes it repairs was widened from parse errors
to every defect **local to one file**: `syntax_error`, `invalid_json`, `empty`,
`not_html`, `unbalanced_braces`, `stray_marker`. Defects that are facts about the whole
tree — a missing peer, an undeclared id, an unresolved import — stay with the end-of-run
review, because repairing those per file made a run spin on a stylesheet that was waiting
for the next component.

The per-depth step doubling in `review.py` and the `REPAIR_ROUNDS * 2` in the orchestrator
are gone: with a real step budget, "ultra gets twice as many" is noise. `REPAIR_ROUNDS`
moved 1 → 3 for every depth.

### Peer context: 4 files × 320 tokens → all files × 1,600 tokens

`context.files_context`. The context that keeps ids, classes and imports consistent across
files was hardcoded to four files at 320 tokens each — about a fifth of a real page. That
is not enough to do the job the function exists for, so the writer guessed. Now
`HARNESS_PEER_FILES` (0 = no cap) and `HARNESS_PEER_TOKENS`.

### Findings: 700 → 2,400 characters

`review.py`. A verifier finding with a file, a code and a detail is about 120 characters,
so 700 held five and the sixth defect was invisible to the model asked to fix it. The
whole block is already bounded at twelve issues by `verify.as_findings`; this second cut
was the tighter one. The rewrite fallback's view of the current file moved 7,000 → 24,000
characters for the same reason.

### The always-on budget is now checked

`prompts.over_always_on_budget()` was measured and tested and never called by the harness,
which made the 24,000-character cap a number in a test rather than a fact about a run. The
orchestrator now emits it once at start when the standing text is over.

### The prompts say the model may think

`prompts.REASON` gained an explicit instruction that the plan is where the design is
decided, and that a simple file should say so rather than pad the stages.
`prompts.CODE` gained the instruction that a complete file beats a terse one, and that a
file cut off mid-line is worse than a shorter file that ends.

## What this does not fix

- **The build is still one-shot.** The model gets tools when a check fails, not while it
  writes. A build-time tool loop — let the author read the tree and check its own file
  before declaring it written — is the next real step, and it is a structural change to
  `write_unit`, not a setting.
- **The planner is still bounded by `PLAN_SECONDS` (120s).** A slow local model falls back
  to the inferred stack. That is a defensible default and it is unchanged.
- **`MAX_FILE_LINES` (2,000) is a soft limit** — it clips what a *reader* sees, not what a
  writer may produce. The byte quota in `agent_loop._write` is the hard stop.
- **A local model with a small context cannot use a 32k output ceiling.** The adapters
  degrade to the provider's own default rather than failing, but the real fix is a
  per-model ceiling, which needs a field the roster does not carry.

## Verification

- `pytest`: 497 passed, 3 skipped (excluding `test_browser.py`, which drives real Chrome
  and is flaky on this machine, and `test_auto_preview.py`, which is another change in
  flight and fails on a clean checkout too).
- `ruff check .`: clean. `ruff format --check`: clean.
- `python -m ai_harness.evals --compare`: every case at full score, none below baseline.
- `npm run typecheck`: clean.
