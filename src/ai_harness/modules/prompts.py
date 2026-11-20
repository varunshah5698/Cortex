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
 "files":["package.json","src/main.tsx","src/App.tsx","src/index.css"],
 "work_units":[{"title":"short name","role":"frontend|backend|general","file":"src/App.tsx",
                "objective":"what exactly this file must contain"}],
 "research_questions":["a question worth searching the web for","..."]}

Rules:
- 2 to 4 files, real paths with the correct extensions. Nested folders are fine.
- Include a manifest when the stack needs one (package.json, requirements.txt, pyproject.toml, go.mod).
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
The team is building: "{prompt}"

Peer interfaces you must honour:
{contracts}

Research notes:
{notes}

Think before writing. Use these stages, each as a heading:
1. RESTATE — what this file must do, in your own words.
2. QUESTIONS — what is ambiguous, and how you resolve it yourself.
3. OPTIONS — two candidate designs, with the trade-off.
4. DECISION — which design and why.
5. RISKS — edge cases and failure states.
6. BLUEPRINT — the sections, functions, components or ids you will write, in order.
7. SELF-CHECK — what a reviewer would criticise.

Be concrete: name real ids, classes and functions. No code. Stay under {words} words."""

# Pass 2 — code, grounded in the agent's own reasoning.
CODE = """You are {name}, the {role} specialist writing {file} for: "{prompt}"

Your own plan, which you must follow:
{reasoning}

Peer interfaces:
{contracts}

{progress}

Write the complete file. No TODOs, no placeholders, no commentary outside the file.

Output format, exactly:
<<<FILE {file}>>>
```language
...complete file...
```"""

REVIEW = """You are the reviewer. The request was: "{prompt}"
Read every file and mentally run what was built: the entry point, the imports, the wiring, the
configuration, and whatever is missing for the stack that was chosen (markup and handlers for a
web page, headers and routing for a service, argv and exit codes for a CLI).

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

SYNTHESIS = """You are the synthesizer. In under 100 words: what the team built, who wrote which file,
how the files connect, and the one limitation that remains. Plain text."""

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
5. SELF-CHECK — ask yourself directly: "Am I answering what was asked, or drifting into something
   else?" Answer honestly, and correct the drift now if there is one.
6. OUTLINE — the shape of the answer you will give.

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
