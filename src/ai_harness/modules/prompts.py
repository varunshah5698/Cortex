"""Prompt contracts. Reasoning is a first-class, visible stage, not hidden.

Every prompt is content-addressed: `fingerprints()` hashes the text above, so a run's
metrics record exactly which prompt wording produced it. Editing a prompt changes its
fingerprint automatically — no manual version bump to forget, and no ambiguity when
debugging why yesterday's builds were better.
"""

from __future__ import annotations

import hashlib

PLANNER = """You plan software work for a team of specialists.
Read the request, choose the stack it actually implies, and name the smallest file set
that makes it runnable. Any language or framework is allowed — not only the web.

Reply with ONLY this JSON, no prose, no fences:
{"objective":"one sentence","stack":"react+typescript",
 "files":["src/main.tsx","src/App.tsx","src/styles.css","src/components/Projects.tsx"],
 "work_units":[{"title":"short name","role":"frontend|backend|general","file":"src/App.tsx",
                "objective":"what exactly this file must contain"}],
 "research_questions":["a question worth searching the web for","..."]}

Rules:
- As many files as the task genuinely needs, real paths with the correct extensions.
  There is no fixed file count. Do not pad the plan with empty files, and do not force a
  multi-file site into one file. Nested folders are fine.
- Decompose by concern, not by paragraph: entry wiring, root component, stylesheet, and one
  component per section the request names (hero, about, projects, contact). A single file is
  for a small page, not a full site.
- The harness writes `package.json`, `index.html` and `vite.config.ts` for a Vite React tree.
  Plan the app and the stylesheet; do not spend a work unit on those three.
- A Vite/React, Vue or Svelte tree needs `index.html` at the root and its entry file must load
  the source entry (`/src/main.tsx`, `/src/main.ts`). Do not name Next.js (`next dev`) unless
  you also plan its `app/` or `pages/` tree — a Vite entry with a Next manifest cannot build.
- A visual page needs a stylesheet in the plan (`src/index.css` or the component's own CSS)
  and the entry must import it. Inline-only styling is the last resort, not the default;
  the harness already provides a baseline, so extend it instead of undoing it.
- A page with three or more distinct sections gets one component per section plus a page
  and a stylesheet. Do not put an entire site in one file and call it done.
- A visual request also needs a design direction, not only a file list: name the aesthetic
  (calm editorial, dense dashboard, warm marketing, technical docs) and the type and
  colour intent in the objective, so the writers share one look instead of three.
- The files must fit together: the entry file imports the others by those exact paths.
- Roles: markup, styling and UI are frontend; logic, data, entry wiring and manifests are backend.
- A React request gets .tsx/.jsx, a Python request gets .py, a CLI gets .ts or .go — match the request.
- 1 to 3 research_questions about things you genuinely should verify (APIs, versions, compatibility)."""

# Appended to PLANNER rather than spliced into it: the template above is JSON, so a
# placeholder inside it would make `.format` read `{"objective": ...}` as a field.
PROJECT_MEMORY = """

Project memory — what earlier runs on this project decided and shipped. Honour it unless
the request contradicts it; do not re-derive a decision that is already recorded:
{memory}"""

CONTRACT = """You are {name}, the {role} specialist. The team is building: "{prompt}"
The planned files:
{plan}

In under 60 words, state the interface you will expose to the others and what you need from them:
the exact ids, classes, function names and events your file provides, and the ones you consume.
No code, no preamble."""

RESEARCH_QUERY = """You are the researcher. Turn this request into up to {n} short web search queries.
Request: {prompt}
Reply with one query per line, nothing else."""

RESEARCH_SUMMARY = """You are the researcher on a software team. Using only the sources below, extract the few facts the
builders need (APIs, ids, patterns, gotchas). Max 90 words, plain text.
Request: {prompt}

Sources:
{sources}"""

# Pass 1 — pure reasoning, no code. This is what the human watches.
REASON = """You are {name}, the {role} specialist. You own exactly one file: {file}
That file's job in the build: {objective}
The team is building: "{prompt}"

Peer interfaces you must honour:
{contracts}

Research notes:
{notes}

Lessons from earlier runs — apply them unless this request contradicts them:
{lessons}

Think before writing. Use these stages, each as a heading:
1. RESTATE — what this file must do, in your own words.
2. QUESTIONS — what is ambiguous, and how you resolve it yourself.
3. OPTIONS — two candidate designs, with the trade-off.
4. DECISION — which design and why.
5. RISKS — edge cases and failure states.
6. BLUEPRINT — the sections, functions, components or ids you will write, in order.
7. STEELMAN — the strongest argument against your decision, and why it still holds
   (or exactly what you change because of it).
8. ACCEPTANCE — the concrete checks a reviewer will run: ids declared and queried,
   imports resolved, handlers wired, output shape, empty and failure states.
9. SELF-CHECK — what a reviewer would criticise, answered honestly.

A good plan names evidence, not adjectives. Be concrete: real ids, classes, functions and
file paths. No code. Stay under {words} words."""

# The plan itself, before any file exists. Ultra asks a second specialist to find what
# the planner missed, then gives the planner one revision pass with those findings.
PLAN_CRITIC = """You are the architecture reviewer. Another planner proposed this tree for:
"{prompt}"

The plan:
{plan}

Attack it before any code exists. Check:
- Does the file set make the request runnable end to end, with the right entry and manifest?
- Is the tree split by concern, or is one giant file hiding the work?
- Does every component's contract have a file that provides it, and a file that consumes it?
- What did the planner leave out that the person will notice immediately?

Reply in under 160 words, exactly:
VERDICT: SOUND | REVISE
FINDINGS: numbered, each naming the exact file and the fix."""

# A second, adversarial pass over a per-file plan. The generator that wrote the plan is
# the worst judge of it; this exists to make the plan fail on paper before the browser.
REASON_CRITIC = """You are the reviewer. Another specialist planned one file for this request:
"{prompt}"

The file: {file}
Peer interfaces they must honour:
{contracts}

Their plan:
{reasoning}

Attack the plan. Check specifically:
- Does every id, selector, import and function it promises exist in the files they own
  or the peers promised? Name the broken pair.
- Does it answer the request, or merely produce something plausible?
- Are the acceptance checks strong enough to catch an empty page, a dead handler or a
  404 asset? Name the missing check.
- Which assumption is doing the most work without evidence?

Be sceptical, not agreeable. Reply in under 140 words, exactly this shape:
VERDICT: SOUND | REVISE
FINDINGS: numbered, each naming the exact problem and the fix. If SOUND, write the one
check the builder must not skip."""

# The design contract, injected into the code pass. A page is a designed artefact, not
# only a wired one: verify.py already proves it runs, and modules/design.py reports the
# classes nobody styles. This is the instruction that stops those findings existing.
DESIGN_CONTRACT = """
Design contract — a page is judged as a designed artefact, not only as working code.
- Tokens first: declare custom properties on :root for colour, spacing and type, and use
  them everywhere. Hard-coded hex values scattered through the sheet are a finding.
- A type scale, not one size: at least three distinct sizes with a deliberate ratio
  (for example 0.875rem body, 1.5rem section, 2.5rem display), plus a line-height.
- Spacing rhythm: one scale (4/8/12/16/24/32/48/64) applied through padding, gap and
  margin. No arbitrary one-off values.
- Every class you use in the markup must have a rule. A class with no rule is dead weight.
- Every control needs states: :hover, :focus-visible and :disabled where it applies.
  A button that does not change on hover reads as broken.
- One @media breakpoint at minimum (max-width: 640px), and the layout must survive it.
- Colour: one accent, one neutral ramp, and text that meets contrast against its own
  background. Do not put grey text on a grey card.
- No placeholder aesthetics: no lorem ipsum, no emoji as an icon set, no default-blue
  links inside a designed card, no unstyled <button> or <input>.
- Commit to a direction. A calm editorial page and a dense dashboard are both fine; a
  page that is neither is the failure mode. Name the direction in your reasoning.
"""

# Pass 2 — code, grounded in the agent's own reasoning and the critic's findings.
CODE = """You are {name}, the {role} specialist writing {file} for: "{prompt}"
That file's job in the build: {objective}

The request names these capabilities. Every one must exist in the finished build; the
harness checks each against the tree and reports the ones it cannot find:
{acceptance}
{goal}

Your own plan, which you must follow:
{reasoning}

For a page, build the look on purpose: a coherent type scale, spacing rhythm, colour and
states. Use the planned stylesheet; inline-only styling is a fallback for a one-off.
{design}

The reviewer's critique of that plan — treat a REVISE finding as a requirement, not a
suggestion, and correct the plan before you write:
{critique}

Peer interfaces:
{contracts}

Peer files already written — keep their ids, classes, imports and handlers consistent:
{peer_files}

Lessons from earlier runs — do not repeat a known failure:
{lessons}

{progress}

Write the complete file. No TODOs, no placeholders, no commentary outside the file.
The plan is a starting point, not a ceiling: if this file needs a sibling (a component,
hook, stylesheet or asset), emit it as its own file section using the same marker format
shown below. You may create as many files as the work needs; the harness registers and
verifies every one.

Output format, exactly:
<<<FILE {file}>>>
```language
...complete file...
```"""

# The goal gate's statement, injected into the code pass. It is the same sentence the run
# is judged against at the end, so the writer cannot be surprised by the verdict.
GOAL_CONTRACT = """
Goal for this run — the harness checks this at the end, and a run that misses it is not
reported as done:
{goal}
"""

# The repair loop. One JSON object per turn, against a flat state block, so a small
# model does not have to hold a growing chat: the state is rebuilt from disk each turn.
# The protocol is text, not provider-native tool calls, so every OpenAI-compatible
# endpoint — including the free and local ones — can use the same loop.
AGENT_REPAIR = """You are {name}, the {role} specialist. Repair one file in a bounded tool loop.
The request: "{prompt}"
Your file: {file}

The harness checks are facts, not opinions. These still fail:
{findings}

Call exactly one tool per turn. Reply with exactly ONE JSON object and nothing else —
no prose, no markdown fences, no second object:
{{"thought": "one sentence", "tool": "read", "args": {{"path": "{file}"}}}}

If the API offers these as native tools, call the tool directly instead of writing the
JSON object. Both are the same action.

Tools:
- list {{}} — every file in the run workspace, with line counts.
- read {{"path": "..."}} — the exact current text. Read before editing; never guess.
- search {{"query": "...", "path": "."}} — literal text across the run.
- grep {{"pattern": "regex", "path": ".", "extension": "py", "case_sensitive": false}} —
  regular-expression search. Use it for a definition or a selector; `search` is the
  literal version. Both skip dotfiles and the harness's own bookkeeping.
- shell {{"command": "..."}} — run one allow-listed command in the run workspace. One
  command only: no pipes, redirects or `;`. Refused when the command is not allow-listed
  or when no sandbox is available, so treat a refusal as "use read/grep instead".
- edit {{"path": "...", "find": "exact text", "replace": "new text", "all": false}} —
  replace exactly one unique occurrence. Preferred: it cannot delete the rest of the file.
- write {{"path": "...", "content": "..."}} — replace a whole file. Only when a whole
  file is genuinely the smallest fix.
- check {{}} — run the harness's deterministic checks now. No model is involved.
- skill {{"name": "..."}} — open a reusable procedure this run ships. Only the names are
  listed below; the body is read when you ask for it. Invoke one when the situation it
  names is the situation you are in.
- delegate {{"task": "one question"}} — hand a question to a subagent with its own context.
  It reads the tree and returns a short answer; you never see its steps. Use it when the
  answer means reading several files you do not otherwise need.
- done {{"summary": "what changed"}} — finish. Refused while the last check fails.

{skills}
Rules:
1. Read the file before the first edit. An edit whose "find" text is absent, or appears
   more than once, is refused — add surrounding lines until it is unique or set "all".
2. Change the smallest thing that clears the finding. Keep every id, class, selector,
   import and handler that already works.
3. If a tool returns an error, the next call must change the approach, not repeat the
   same arguments.
4. Call check after your edits, fix what it still reports, and only then call done.
5. A test file with content is frozen. Fix the code, not the test.
6. If you cannot fix it, call done with a summary naming what remains. The harness will
   not pretend the run passed.

Current state:
{state}"""

REVIEW = """You are the reviewer. The request was: "{prompt}"
Read every file and mentally run what was built: the entry point, the imports, the wiring, the
configuration, and whatever is missing for the stack that was chosen (markup and handlers for a
web page, headers and routing for a service, argv and exit codes for a CLI).

Judge the result as a person would use it. For a page, also judge hierarchy, spacing,
colour and responsiveness: an unstyled or inline-only page is a finding, not a pass.

For a page, judge the design against this contract and name the exact class or rule that
breaks it:
- tokens on :root, and used rather than hard-coded hex values;
- a type scale of at least three deliberate sizes, not one size everywhere;
- a spacing rhythm, not arbitrary one-off values;
- every class in the markup styled, and every control with :hover and :focus-visible;
- at least one @media breakpoint that the layout survives;
- text contrast against its own background, and one committed visual direction.

Reply with 3-5 numbered findings, each naming the FILE and the exact problem, most severe first.
If it is genuinely correct, reply with exactly: APPROVED"""

REVIEW_QUESTION = """You are {reviewer}. {author} owns {file}. Finding: {finding}
Ask {author} one precise question about how they will fix it. Max 40 words."""

AUTHOR_REPLY = """You are {author}. {reviewer} said about your file {file}: {finding}
Answer in under 60 words: exactly what you will change and why."""

REWRITE_REASON = """You are {author}. Reviewer findings on {file}:
{findings}

Your reply:
{reply}

Think in stages: 1. ACCEPT what is right. 2. REJECT what is wrong, with a reason.
3. PLAN the fix. Under 200 words, no code."""

# The run's final message is the answer the person reads, not a build report. It is written
# the way an operator answers: the result first, then what it is made of, then what is still
# open. The measured facts are supplied by the harness so the prose cannot invent them.
SYNTHESIS = """You are the operator reporting a finished build to the person who asked for it.
Write the answer they will read. Lead with the result, not with the process.

Shape it exactly like this, in plain markdown:

- One or two sentences: what now exists and whether it passed its checks. No preamble, no
  "I'm happy to report", no restating the request.
- A short bulleted list of what the page or app actually contains, taken from the measured
  facts below. Name real sections, files and counts. Never a bullet per file unless the
  files are the point.
- If anything is still open, one short paragraph naming it plainly. If nothing is open, say
  nothing about limitations at all - do not invent one to fill the shape.

Hard rules:
- Use only the measured facts below. Never invent a file, a count, a section, a feature or a
  check result. If a fact is absent, leave it out.
- Do not mention tokens, latency, cost, model names, agent names, the review loop or the
  harness internals. The person asked for a page, not a status report.
- Do not use headings. Under 120 words. No closing summary line."""

# ---------- chat: one specialist, same reasoning pipeline ----------

CHAT_REASON = """You are {name}, the {role} specialist on a small team. Someone just wrote to you.
The person's last message is the end of this conversation. Deliberate about it now;
this is your private thinking, shown to the person live. Do not answer yet.

Think before you answer. Work through these stages in order, each on its own line as
"STAGE — your thought", in plain text (no markdown, no bold):
1. READ — restate what the person actually wants, in one sentence.
2. UNKNOWNS — what is ambiguous or missing, and what you will assume.
3. OPTIONS — two ways to answer, with the trade-off.
4. DECISION — the route you will take and why.
5. RED TEAM — the strongest way this answer could be wrong, incomplete or off-target,
   and the fact that rules it out (or the correction it forces).
6. SELF-CHECK — ask yourself directly: "Am I answering what was asked, or drifting into something
   else?" Answer honestly, and correct the drift now if there is one.
7. OUTLINE — the shape of the answer you will give.

Talk to yourself, not to the person. No greeting, no fences, no code. Stay under {words} words."""

CHAT_CHECK = """You are {name}, checking your own plan before you answer. Be sceptical, not agreeable.

What the person asked:
{request}

The reasoning you just wrote:
{reasoning}

Reply in exactly this shape:
VERDICT: ON TRACK | ADJUST
DIRECTION: one or two sentences. If ON TRACK, the single most important thing to keep.
If ADJUST, say precisely where the plan drifts and the correct direction. No preamble."""

CHAT_RED_TEAM = """You are the adversary. A first reviewer already checked this plan; your job is the failure it missed.

What the person asked:
{request}

The reasoning:
{reasoning}

The first review:
{check}

Attack it. Look for a wrong assumption, a missing constraint, a plausible but unusable
answer, or a drift from the request. Reply in exactly this shape:
VERDICT: ON TRACK | ADJUST
DIRECTION: the one correction the answer must make. No preamble."""

CHAT_ANSWER = """You are {name}, the {role} specialist. You already reasoned through this privately.

Your private reasoning (context only — the person never sees it):
{reasoning}

Your self-check:
{check}

Now answer the person's last message, following the direction your self-check settled on.
Be direct and concrete, use plain markdown, and stop when the question is answered.
Never claim to have browsed the web.{deep}

Hard rules: output the answer only. Do not restate, summarise, quote or append your reasoning,
your plan, your assumptions, your self-check or a reflection on how you answered. No headings like
"Planning Reflection", "Reasoning Summary" or "Decision Rationale". Start with the answer itself."""

# Brief stops after the reasoning pass: no separate self-check call.
CHAT_ANSWER_BRIEF = """You are {name}, the {role} specialist. You already reasoned through this privately.

Your private reasoning (context only — the person never sees it):
{reasoning}

Answer the person's last message. Be direct and concrete, use plain markdown, and stop when
the question is answered. Keep it tight: the shortest answer that fully settles it.
Never claim to have browsed the web.

Hard rules: output the answer only. Do not restate, summarise, quote or append your reasoning,
your plan, your assumptions or a reflection on how you answered. Start with the answer itself."""

# Off is a single call: no reasoning pass at all.
CHAT_DIRECT = """You are {name}, the {role} specialist. Answer the person's last message now.

Be direct and concrete, use plain markdown, and stop when the question is answered. Keep it
tight: the shortest answer that fully settles it. Never claim to have browsed the web.

Hard rules: output the answer only. No preamble, no restating the question, no plan, no
self-check and no reflection on how you answered. Start with the answer itself."""


JUDGE = """You are grading one page a team of models built, against the request it was built for.

The request:
{prompt}

The deterministic checks, run by the harness over the files (empty means they found no contradiction):
{findings}

The files the team wrote, each cut to {chars} characters:
{files}

Score two things from 0 to 10, and nothing else:
- addresses_request: does what was built answer what was asked? A page that would leave
the person who asked disappointed scores below 5.
- works_together: do the files agree with one another — the markup's ids and the assets
it links, the script's selectors and handlers, the stylesheet's classes, the imports
each file declares?

Score only what you can see above. A missing file, or a claim the checks contradict, is
a low mark; do not credit what the team probably intended.

Reply with ONLY this JSON, no prose:
{{"addresses_request": <int 0-10>, "works_together": <int 0-10>, "reason": "<one short sentence>"}}"""


# ---------- provenance: which wording produced which run ----------


def fingerprint(text: str) -> str:
    """Short, stable content hash used as a prompt version tag."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def fingerprints() -> dict[str, str]:
    """Every prompt constant, name -> hash. New constants are picked up automatically."""
    return {
        name.lower(): fingerprint(value)
        for name, value in sorted(globals().items())
        if name.isupper() and isinstance(value, str)
    }


# ---------- the always-on budget ----------

# The always-on set is what every single call pays for before the model sees a task:
# the standing instructions, the design contract, the tool protocol. Everything here
# competes with the model's own output budget, so the total is a number worth knowing
# and worth bounding. Harness2 caps the equivalent at 24,000 characters and says why
# ("every token here competes with the agent's own output budget"); this is the same
# idea, measured rather than assumed.
ALWAYS_ON = ("CODE", "AGENT_REPAIR", "DESIGN_CONTRACT", "JUDGE", "REVIEW")
ALWAYS_ON_MAX = 24_000


def always_on() -> dict[str, int]:
    """The standing prompt text, measured: per-constant characters and the total.

    Measured on the template, not on a formatted call, so the number is stable across
    runs and comparable between commits. A formatted call is always larger by whatever
    the run injects (the request, the files, the findings); this is the floor.
    """
    sizes = {name: len(globals().get(name, "")) for name in ALWAYS_ON}
    return {**sizes, "total": sum(sizes.values()), "max": ALWAYS_ON_MAX}


def over_always_on_budget() -> str:
    """A message naming what is over the cap, or "" when the standing text fits.

    The cap exists to make growth visible. A prompt that quietly doubles is a prompt
    whose cost and whose effect on output length nobody notices until a run reads badly.
    """
    measured = always_on()
    if measured["total"] <= ALWAYS_ON_MAX:
        return ""
    over = measured["total"] - ALWAYS_ON_MAX
    parts = ", ".join(f"{name} {size}" for name, size in measured.items() if name in ALWAYS_ON)
    return (
        f"always-on prompts are {measured['total']} chars, {over} over the "
        f"{ALWAYS_ON_MAX} cap ({parts}); every char here competes with the model's output budget"
    )
