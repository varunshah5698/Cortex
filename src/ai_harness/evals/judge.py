"""The second eval tier: a rubric over what shipped, not over whether it runs.

The deterministic tier answers facts — did the run finish, did the verifier reach its
verdict, are the declared files non-empty, did the tree compile (`--build`). None of
those asks whether the page is any good, and a model's opinion is too unstable to be a
gate. So this tier is **advisory only**: `--judge` prints a score and writes it into the
result, and `evals/__main__.py` never reads it for the exit status. The deterministic
checks stay the gate.

Three things keep the opinion as steady as an opinion can be:

* the rubric is pinned to `temperature=0`, so one artifact gets one verdict, and the
  prompt is a constant in `modules/prompts.py` and therefore content-hashed with the
  rest (`prompts.fingerprints()` records which wording graded the run);
* the reviewer grades, so the model scoring the work is the one the run already trusted
  to review it, not a new specialist with its own habits;
* the request, the files and the verifier's findings all go in the prompt, so a claim the
  files contradict is visible to the judge rather than guessed at.

The score is the lower of the two marks, because a page that ignores the request and a
page whose files disagree are both failures and neither average should hide one.
"""

from __future__ import annotations

import json

from ..core.state import RunState
from ..models import Specialist
from ..modules import llm, prompts
from ..nodes import verify
from .cases import Case

FILE_CHARS = 2000
REASON_CHARS = 200
CLEAN_FINDINGS = "(the deterministic checks found no contradiction)"


def judge_agent(run: RunState) -> Specialist:
    """The reviewer grades; a roster without one falls back to its first specialist."""
    return next((s for s in run.specialists if s.role == "reviewer"), run.specialists[0])


def files_context(run: RunState) -> str:
    """The written files, each cut to FILE_CHARS, in a form a model can read."""
    blocks: list[str] = []
    for path, meta in sorted(run.files.items()):
        body = str(meta.get("content") or "")
        if len(body) > FILE_CHARS:
            body = body[:FILE_CHARS] + f"\n…[{len(body) - FILE_CHARS} characters cut]"
        blocks.append(f"<<<FILE {path}>>>\n{body}")
    return "\n\n".join(blocks) or "(no files were written)"


def prompt(case: Case, run: RunState) -> str:
    """The rubric, filled with the request, the findings and the files."""
    findings = verify.as_findings(run.verification or {}) or CLEAN_FINDINGS
    return prompts.JUDGE.format(
        prompt=case.prompt,
        findings=findings,
        files=files_context(run),
        chars=FILE_CHARS,
    )


def parse(text: str) -> dict | None:
    """The two marks and the reason, or None when the answer holds no readable score."""
    try:
        body = json.loads(text[text.index("{") : text.rindex("}") + 1])
        asked = max(0, min(10, int(body["addresses_request"])))
        together = max(0, min(10, int(body["works_together"])))
    except (ValueError, KeyError, TypeError, IndexError):
        return None
    return {
        "value": round(min(asked, together) / 10, 2),
        "addresses_request": asked,
        "works_together": together,
        "reason": str(body.get("reason") or "")[:REASON_CHARS],
    }


async def score(case: Case, run: RunState, spec: Specialist) -> dict:
    """One rubric call about this run. Never raises, never gates anything."""
    messages = [
        {"role": "system", "content": prompt(case, run)},
        {"role": "user", "content": f"Score the build for: {case.prompt}"},
    ]
    result = {
        "case": case.name,
        "judged_by": spec.name,
        "model": spec.model,
        "value": None,
        "addresses_request": None,
        "works_together": None,
        "reason": "",
        "error": "",
        "usage": {},
    }
    try:
        text, usage = await llm.call(spec, messages, temperature=0)
    except llm.ProviderError as exc:
        return {**result, "error": f"{type(exc).__name__}: {exc}"[:REASON_CHARS]}
    result["usage"] = usage.as_dict()

    verdict = parse(text)
    if verdict is None:
        return {**result, "error": "the judge answered without a readable score"}
    return {**result, **verdict}
