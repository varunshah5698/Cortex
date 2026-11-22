"""The evaluator rubric: grade the session, not only the tree.

Every other gate in this harness asks a question about the *artifact*: does it parse, does
the verifier accept it, is the requested capability present, is the goal met. None of them
asks the question Lecture 11 puts at the centre of observability — *was this a good
session?* A run can pass every mechanical check while leaving a half-finished step
undocumented, drifting out of scope, or producing something the next session cannot pick
up.

So the rubric scores six categories 0-2 and turns the total into one of three verdicts:

| Category | Question |
| --- | --- |
| Correctness | Does the behaviour match the requested feature? |
| Verification | Did the required checks actually run, with evidence? |
| Scope discipline | Did the session stay inside the chosen scope? |
| Reliability | Does the result survive a restart without repair? |
| Maintainability | Is it clear enough for the next session? |
| Handoff readiness | Can a fresh session continue from repo artifacts alone? |

Two design rules keep it honest:

* **It is not the author.** The evaluator is a different specialist, and it is given the
  measured facts, not the author's summary of them.
* **It cannot invent a verdict.** A malformed or missing answer is `unknown`, never
  `accept`. A rubric that defaults to approval is a rubber stamp with extra steps.

The rubric is opt-in (`HARNESS_RUBRIC`), because it is a model call and the mechanical
gates already refuse a broken run. When it is on, a `block` verdict refuses the stop the
same way an unmet goal does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config.settings import RUBRIC_ENABLED

ACCEPT = "accept"
REVISE = "revise"
BLOCK = "block"
VERDICTS = (ACCEPT, REVISE, BLOCK)
UNKNOWN = "unknown"

CATEGORIES = (
    ("correctness", "Does the implemented behavior match the requested feature?"),
    ("verification", "Did the required checks actually run, with evidence?"),
    ("scope", "Did the session stay inside the chosen feature scope?"),
    ("reliability", "Does the result survive restart or rerun without repair?"),
    ("maintainability", "Is the code and documentation clear enough for the next session?"),
    ("handoff", "Can a fresh session continue work from repo artifacts only?"),
)

# A total out of 12. Below `REVISE_AT` the session needs another pass; below `BLOCK_AT` it
# should not be reported as finished at all.
REVISE_AT = 8
BLOCK_AT = 5

SYSTEM = """You are grading a completed session of an AI coding harness. You did not write
this work. Score each category 0, 1 or 2 using only the measured facts you are given.

{categories}

Answer in exactly this form, one line per category, then a verdict line:

correctness: <0-2> — <one short reason>
verification: <0-2> — <one short reason>
scope: <0-2> — <one short reason>
reliability: <0-2> — <one short reason>
maintainability: <0-2> — <one short reason>
handoff: <0-2> — <one short reason>
VERDICT: ACCEPT | REVISE | BLOCK
MISSING: <the single most important thing missing, or "nothing">

ACCEPT means you would ship it. REVISE means one bounded pass would fix it. BLOCK means it
must not be reported as finished. Do not award a 2 to a category the facts do not support.
"""


@dataclass
class Score:
    """One rubric result: the six scores, the total, and the verdict they imply."""

    scores: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    verdict: str = UNKNOWN
    missing: str = ""
    total: int = 0
    by: str = "none"

    def public(self) -> dict:
        return {
            "scores": self.scores,
            "reasons": self.reasons,
            "verdict": self.verdict,
            "missing": self.missing,
            "total": self.total,
            "by": self.by,
        }


def _categories_block() -> str:
    return "\n".join(f"- {name}: {question}" for name, question in CATEGORIES)


def verdict_for(total: int) -> str:
    """The verdict a total implies. Kept separate so it can be tested without a model."""
    if total < BLOCK_AT:
        return BLOCK
    if total < REVISE_AT:
        return REVISE
    return ACCEPT


def parse(raw: str) -> Score:
    """Read the evaluator's answer. A malformed answer is `unknown`, never `accept`."""
    score = Score(by="evaluator")
    text = str(raw or "")
    for name, _question in CATEGORIES:
        match = re.search(rf"^\s*{name}\s*:\s*([0-2])\s*(?:[—:-]\s*(.*))?$", text, re.I | re.M)
        if not match:
            continue
        score.scores[name] = int(match.group(1))
        reason = (match.group(2) or "").strip()
        if reason:
            score.reasons[name] = reason[:200]
    verdict_match = re.search(r"VERDICT\s*:\s*(ACCEPT|REVISE|BLOCK)", text, re.I)
    missing_match = re.search(r"MISSING\s*:\s*(.+)", text, re.I)
    if missing_match:
        score.missing = missing_match.group(1).strip()[:300]

    # A partial answer is not a grade. Every category must be scored, or the total would
    # silently be computed over a subset and read as a pass.
    if len(score.scores) < len(CATEGORIES):
        score.verdict = UNKNOWN
        score.total = sum(score.scores.values())
        return score

    score.total = sum(score.scores.values())
    implied = verdict_for(score.total)
    stated = verdict_match.group(1).lower() if verdict_match else ""
    # The total is the harness's answer; the model's stated verdict may only be stricter,
    # never more generous. A model that scores 3/12 and says ACCEPT does not get to ship.
    if stated in VERDICTS and VERDICTS.index(stated) > VERDICTS.index(implied):
        score.verdict = stated
    else:
        score.verdict = implied
    return score


def prompt() -> str:
    return SYSTEM.format(categories=_categories_block())


async def evaluate(run, agent, ask) -> Score:
    """Ask `agent` to grade the run. Returns `unknown` when the rubric is off or silent."""
    if not RUBRIC_ENABLED or ask is None:
        return Score(by="none")
    facts = _facts(run)
    try:
        raw = await ask(prompt(), f"Measured facts for this session:\n{facts}")
    except Exception as exc:  # noqa: BLE001 - a failed grade must not fail the run
        score = Score(by="evaluator")
        score.missing = f"evaluator failed: {type(exc).__name__}: {exc}"[:300]
        return score
    score = parse(raw)
    if score.verdict == UNKNOWN and not score.missing:
        score.missing = "the evaluator did not score every category"
    return score


def _facts(run) -> str:
    """What the evaluator may draw on: the harness's own measurements, never prose."""
    lines: list[str] = []
    report = getattr(run, "verification", None) or {}
    if report:
        state = "passed" if report.get("ok") else f"failed with {report.get('errors', 0)} error(s)"
        lines.append(f"Deterministic checks: {state} over {report.get('checked', 0)} file(s).")
        for item in (report.get("issues") or [])[:6]:
            lines.append(f"  issue: {item.get('level')} {item.get('file')} - {item.get('detail')}")
    goal = getattr(run, "goal", None) or {}
    if goal:
        lines.append(f"Goal gate: {goal.get('status', 'unknown')} (by {goal.get('by', 'none')}).")
    files = getattr(run, "files", {}) or {}
    written = [path for path, meta in files.items() if str(meta.get("content") or "").strip()]
    lines.append(f"Files written: {len(written)} of {len(files)} tracked.")
    planned = list(getattr(run, "planned_files", []) or [])
    if planned:
        missing = [path for path in planned if not str((files.get(path) or {}).get("content") or "").strip()]
        lines.append(f"Planned files: {len(planned)}, unwritten: {len(missing)}.")
    try:
        from . import features as feature_list

        summary = feature_list.summary(run)
        if summary.get("total"):
            lines.append(
                f"Features: {len(summary.get('passing', []))}/{summary['total']} passing, "
                f"active: {summary.get('active') or 'none'}."
            )
    except Exception:  # noqa: BLE001 - facts are best-effort; the grade still runs
        pass
    if getattr(run, "previewed", False):
        lines.append("A preview was served.")
    return "\n".join(lines) or "No measured facts available."
