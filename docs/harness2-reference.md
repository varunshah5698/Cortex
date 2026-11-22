# Harness² — what it is, and what Cortex should take from it

Reference notes on [`google-research/harness2`](https://github.com/google-research/harness2),
read at commit `main` on 2026-11-22. Apache-2.0, Python, 24.7k stars, last upstream push
2026-11-02. Paper: arXiv 2610.14224.

This is a reading document, not a plan. The last section says what I would take and what I
would deliberately leave.

---

## 1. The idea in one paragraph

A **harness** is everything around a model that lets it act as an agent: prompts, tools,
memory, skills, orchestration. **Harnessing** is optimizing that system while the model
weights stay fixed. Harness² runs two levels of recursion. **Task-level recursion**
refines the harness for the task in front of it through a contrastive
*Propose–Probe–Compose* loop: propose edits, probe their effect on the same task, then
compose a refined harness from the behavioral contrast between executions — with no
ground-truth feedback. **Harnessing-level recursion** then updates a persistent
*improvement harness* with the adopted harness and the evidence-supported lesson, so the
next task starts better. Reported gains: up to +11.5 points on WorkBuddy-Bench, +4.5 on
JobBench, +11.8 percentage points on LAB partial pass rate.

The claim that matters for Cortex: **you do not need a better model, you need a better
harness, and you can improve the harness without ground truth by comparing executions
against each other.**

---

## 2. The eight components (c1–c8)

The editable surface is exactly eight things, and the mapping to a concrete file tree is
data, not code. From `harness2/substrate/opencode.py`, the always-on set and the stubs:

| id | component | lives at (OpenCode surface) | role |
|---|---|---|---|
| c1 | system prompt | `systemprompt.md` | always-on context |
| c2 | guardrails | `guardrails.md` | always-on context, and the autotrigger file |
| c3 | memory | `LongTermMEMORY.md` | always-on context |
| c4 | skills | `skills/<name>/SKILL.md` (+ `scripts/`) | loaded on a trigger |
| c5 | subagents | `sub_agents/<name>/AGENT.md` | delegated, own context |
| c6 | tool guidance | `tool_descriptions/tool_guidance.md` | the tool interface |
| c7 | scripts | `scripts/<name>.py` | deterministic helpers |
| c8 | plugins | `plugin/<name>.ts` | acts *inside* the tool loop |

The four always-on files are capped at **24,000 characters combined**, and the reason is
stated in the source: *"every token here competes with the agent's own output budget."*
The Codex surface is the same abstraction with a different shape — `AGENTS.md` alone, and
`reject_empty_always_on=True`, because on that framework an empty always-on file means the
harness does not exist.

The design lesson is in `SurfaceSpec`: every difference between two agent frameworks is
**data** (paths, caps, frontmatter rules, README seeds) plus one per-surface validator.
The read/validate/write/diff logic exists once.

---

## 3. The edit contract

`harness2/core/tree.py` — "The edit contract is omitted-is-inherited: a proposal emits
only what it changes." A proposal is a partial tree; anything absent is inherited from the
current harness. That single rule is what makes a model-authored edit reviewable: the diff
is the proposal.

`validate()` raises one `ValueError` listing **every** problem at once, and returns a
census (`always_on_chars`, `n_files`, `skills`, `sub_agents`, `scripts`, `plugins`,
`hooks`). It checks:

- always-on size against the cap, with the reason quoted in the error
- every skill has a trigger line naming it in the always-on text (or it can never load)
- subagents are well-formed and allowed
- every `scripts/*.py` **compiles** (`_py_compiles`)
- script references resolve, and reserved script names are not shadowed
- hooks match the surface's schema

`diff_summary()` returns added / removed / modified, and `diversity()` measures how much
distinct line-level content a set of candidate trees shares — the anti-collapse metric.

---

## 4. Propose–Probe–Compose, concretely

From `harness2/core/recursion.py`: each mode schedules **4k rollouts and 3k improvement
stages**, including shared base executions. Invalid proposals get **one retry** before
inheriting the current harness (`STAGE_ATTEMPTS = 2`).

- `--k 1` — one Propose–Probe–Compose step.
- `--k K --mode parallel` — probe K general and K domain-specific candidates, then produce
  K compositions from the shared evidence. Explores wider.
- `--k K --mode sequential` — carry the refined harness and accumulated evidence through K
  successive steps. Refines deeper.

`Stage` records what actually happened to each attempt: `kept_base`, `fell_back`, `error`,
`usd`, `attempts`, `cached`, `execution_failed`. `ModeResult.ok` is false if any stage's
execution failed — a refinement built on a broken probe is not a result.

---

## 5. The experience library

`harness2/core/library.py` — the per-domain memory of what earlier tasks produced:

```
<root>/<run-tag>/<mode>/<domain>/INDEX.md
<root>/<run-tag>/<mode>/<domain>/harnesses/<task>/harness/<rel>
<root>/<run-tag>/<mode>/<domain>/harnesses/<task>/EXPERIENCE.md
```

Three properties worth copying:

1. **Retrieval is by construction.** A domain's directory only ever holds same-domain
   entries, so there is no similarity search to get wrong.
2. **The whole tree is carried, never a summary.** A summary of a harness loses exactly
   the detail that made it work.
3. **An experience is only valid if it has all three of `cue`, `lesson`, `evidence`**
   (`has_experience`). A lesson without evidence is rejected, not stored.

---

## 6. Separation of concerns

`harness2/core/spec.py` keeps three axes apart: **method** (recursion, prompts, selection,
library), **substrate** (the agent framework a rollout runs under), and **benchmark** (a
`BenchSpec` plus an adapter). `ChannelSpec` describes the proposer/selector channel
independently of both.

The strictness is deliberate and worth noting: `resolve_channel_model` **raises** if you
override channel and model separately, because "a bare-id/provider-prefixed mismatch fails
silently downstream." It also rejects an `opencode` model id without a provider prefix, and
a `claude_cli` id *with* one. Cheap validation at the seam instead of a confusing failure
three stages later.

`ProposerError` is documented as "a call did not produce a usable reply. Never swallowed
into an empty string." Same instinct as Cortex's fail-closed verification.

---

## 7. What Cortex already has

Cortex is not a beginner version of this. Several of Harness²'s instincts are already
present and in some cases further along:

| Harness² | Cortex equivalent | State |
|---|---|---|
| c1 system prompt | `modules/prompts.py` constants | present, content-hashed |
| c2 guardrails | `modules/guard.py`, `modules/permissions.py` | present, deny-by-default |
| c3 memory | `modules/memory.py` (`learn`, `lessons`) | present |
| c4 skills | `modules/agent_loop.py` tool protocol | partial — tools, not loadable skills |
| c5 subagents | `modules/subagent.py` | present but new |
| c6 tool guidance | `prompts.AGENT_REPAIR` tool block | present |
| c7 scripts | `tools/terminal.py`, `modules/sandbox.py` | present |
| c8 plugins | — | absent |
| validate-before-adopt | `nodes/verify.py::check` | present, stronger |
| edit = diff only | `agent_loop` edit tool | present |
| experience library | `memory.learn` + `evals/regressions.jsonl` | **weaker: flat, not per-domain, no cue/lesson/evidence triple** |
| always-on token cap | — | **absent** |
| harness optimization | — | **absent — this is the real gap** |

---

## 8. What I would take, in order

**1. The always-on budget (c1+c2+c3+c6 under one cap).** Cortex injects
`prompts.CODE` with eleven sections — name, role, file, objective, prompt, acceptance,
goal, reasoning, critique, design, contracts, peer_files, lessons, progress — and nothing
measures the total. Harness² caps the equivalent at 24,000 characters and says why. A
`always_on_chars` figure in the metrics, and a cap that fails loudly, is a small change
with a real effect on output budget.

**2. The `cue` / `lesson` / `evidence` triple for memory.** `memory.learn` currently
accepts a lesson string. Harness²'s `has_experience` refuses to store a lesson that has no
evidence attached. That is exactly the discipline `AGENTS.md` rule 18 asks for ("a
verification failure becomes a durable lesson") but does not enforce structurally.

**3. Per-domain experience retrieval by construction.** Cortex's memory is one flat file
trimmed to `MAX_CHARS`. A per-domain directory keyed on the task's domain, carrying the
whole adopted tree rather than a summary, is the version that does not degrade as it grows.

**4. The validation census.** `validate()` returning `{always_on_chars, n_files, skills,
sub_agents, scripts, plugins}` is a cheap, honest report of what a harness actually is.
Cortex's `verify.check` returns issues; it does not return a census.

## 9. What I would not take

- **The benchmark adapters** (JobBench, LAB, WorkBuddy) — 3,000+ lines tied to datasets
  Cortex does not have.
- **The OpenCode/Codex substrates** — Cortex has its own executor and its own permission
  model, and `AGENTS.md` rule 11 forbids a spawn path that skips `modules/sandbox.py`.
- **The 4k-rollout recursion budget** — that is a research-scale spend. The *idea*
  (probe candidates, compose from contrast) is separable from the scale.
- **The proposer channel plumbing** — `claude_cli` / `opencode` subprocess sessions.
  Cortex already routes through `llm.call`.

## 10. The honest caveat

Harness² optimizes a harness against a **benchmark with a judge**. Cortex's eval suite is
seven golden cases plus two replayed regressions, and its judge tier is explicitly
advisory. Adopting Propose–Probe–Compose without a trustworthy scoring signal would mean
optimizing against noise — the loop would happily converge on a harness that scores well on
seven cases and generalizes to nothing. The eval suite has to get wider before the
optimization loop is worth building. That ordering is the real finding here.
