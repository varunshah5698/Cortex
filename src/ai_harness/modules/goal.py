"""The goal gate: a run may not declare itself finished on its own word.

`core/orchestrator.py` already refuses `done` on a failing verifier, and that is the
strongest guarantee in the harness. It answers a narrower question than the person asked,
though: *does this tree hold together?* not *is this the thing I wanted?* A run can pass
every structural check and still stop early — three of five sections built, a handler that
exists but does nothing, a page whose "search" is an input with no listener.

So a run states a goal up front, and stopping is a claim that has to be checked:

* **The goal is written down before the work starts.** `derive()` reads the request and
  produces a short, checkable statement of what finished looks like. It is persisted as
  `goal.md` beside the run, so a resume judges the same goal.
* **A mechanical check wins.** If the goal names something the verifier can already
  measure — a capability from `modules/intent.py`, a file, a check code — the gate uses
  that and never spends a model call. A model asked "is this done?" says yes.
* **An independent evaluator is the fallback.** Only when no sensor covers the goal does
  the gate ask a model that did not write the work, and it asks for a verdict *and* the
  one thing missing, so a refusal is actionable rather than a shrug.
* **A refusal is bounded.** `GOAL_ROUNDS` is how many times the gate may send a run back.
  After that the run stops and says the goal was not met, which is the honest ending: a
  loop that can always continue needs something outside it to say when to stop.

`evaluate()` never emits `done` and never mutates the run's verdict. It returns a
`Verdict`; the orchestrator decides what to do with it, exactly as it does with the
verifier's report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config.settings import GOAL_ENABLED, GOAL_EVALUATOR
from . import intent

FILENAME = "goal.md"

MET = "met"
UNMET = "unmet"
UNKNOWN = "unknown"


@dataclass
class Verdict:
    """What the gate decided, and the evidence it decided on."""

    status: str = UNKNOWN
    goal: str = ""
    reason: str = ""
    missing: list[str] = field(default_factory=list)
    by: str = "none"  # "sensor" | "evaluator" | "none"

    @property
    def met(self) -> bool:
        return self.status == MET

    def public(self) -> dict:
        return {
            "status": self.status,
            "goal": self.goal,
            "reason": self.reason,
            "missing": self.missing,
            "by": self.by,
        }


def derive(run) -> str:
    """The goal statement for a run, from the request and its acceptance checklist.

    Deliberately mechanical: the acceptance criteria are already the request's named
    capabilities, so the goal is those plus the run's own objective. No model call, which
    means the goal cannot drift from what the person asked for.
    """
    criteria = [str(item.get("label") or "").strip() for item in (getattr(run, "acceptance", None) or [])]
    criteria = [item for item in criteria if item]
    prompt = str(getattr(run, "prompt", "") or "").strip()
    if criteria:
        return f"Deliver what was asked — {prompt[:200]} — including: " + "; ".join(criteria[:8]) + "."
    return f"Deliver what was asked: {prompt[:300]}"


def _sensor_verdict(run, goal: str) -> Verdict | None:
    """A verdict the harness can reach without asking a model, or None if it cannot.

    The acceptance checklist is the sensor: every criterion carries `found`, set by
    `modules/intent.py` reading the tree. The bar is deliberately the *same* bar
    `intent.check` uses for its `intent_unmet` error — several capabilities named and not
    one of them present — because a goal gate that fires on a single missing capability
    warning would block runs the verifier already passed, and a gate that cries wolf gets
    switched off. Anything between "all present" and "none present" is `unknown`: the
    reviewer and the repair loop own that range, not this gate.
    """
    criteria = list(getattr(run, "acceptance", None) or [])
    if not criteria:
        return None
    stats = intent.coverage(dict(getattr(run, "files", None) or {}), criteria)
    hard = int(stats.get("hard") or 0)
    found_hard = int(stats.get("found_hard") or 0)
    if hard >= 2 and found_hard == 0:
        missing = [str(item.get("label") or "") for item in criteria if item.get("hard") and not item.get("found")]
        return Verdict(
            status=UNMET,
            goal=goal,
            reason=f"the request named {hard} capability(ies) and the tree contains a trace of none of them",
            missing=missing,
            by="sensor",
        )
    if hard and found_hard == hard:
        return Verdict(status=MET, goal=goal, reason="every named capability is present in the tree", by="sensor")
    return None


async def evaluate(run, agent, ask) -> Verdict:
    """Decide whether the run met its goal.

    `ask` is an optional `async callable(system, user) -> str` for the model evaluator, so
    this module never imports `llm` and a test can drive it with a scripted answer. When
    it is None, or the evaluator is off, a goal with no sensor answers `unknown` — which
    the orchestrator treats as "do not block", because a gate that cannot see must not
    pretend it did.
    """
    goal = derive(run)
    if not GOAL_ENABLED:
        return Verdict(status=UNKNOWN, goal=goal, reason="the goal gate is off", by="none")

    sensor = _sensor_verdict(run, goal)
    if sensor is not None:
        return sensor

    if not GOAL_EVALUATOR or ask is None:
        return Verdict(
            status=UNKNOWN, goal=goal, reason="no sensor covers this goal and no evaluator is configured", by="none"
        )

    facts = _facts(run)
    system = (
        "You are an independent evaluator. You did not write this work and you are not on "
        "the team that did. Decide one thing: was the goal met?\n\n"
        "Reply in exactly this shape, nothing else:\n"
        "VERDICT: MET | UNMET\n"
        "MISSING: one line per thing that is absent or not working, or NONE\n"
        "REASON: one sentence."
    )
    user = f"The goal:\n{goal}\n\nWhat the harness measured:\n{facts}"
    try:
        answer = await ask(system, user)
    except Exception as exc:  # noqa: BLE001 - an evaluator that fails is not a refusal
        return Verdict(status=UNKNOWN, goal=goal, reason=f"evaluator failed: {type(exc).__name__}: {exc}", by="none")
    return _parse(str(answer or ""), goal)


def _parse(text: str, goal: str) -> Verdict:
    """Read the evaluator's answer. An unreadable answer is `unknown`, never `met`."""
    upper = text.upper()
    if "VERDICT: MET" in upper:
        return Verdict(
            status=MET, goal=goal, reason=_line(text, "REASON") or "the evaluator accepted the goal", by="evaluator"
        )
    if "VERDICT: UNMET" in upper:
        missing = _line(text, "MISSING")
        items = (
            []
            if not missing or missing.upper() == "NONE"
            else [part.strip(" -•") for part in missing.split(";") if part.strip()]
        )
        return Verdict(
            status=UNMET,
            goal=goal,
            reason=_line(text, "REASON") or "the evaluator did not accept the goal",
            missing=items,
            by="evaluator",
        )
    return Verdict(status=UNKNOWN, goal=goal, reason="the evaluator's answer had no readable verdict", by="evaluator")


def _line(text: str, label: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(f"{label}:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def _facts(run) -> str:
    """What the evaluator is allowed to judge on: the tree and the checks, not a claim."""
    lines: list[str] = []
    report = getattr(run, "verification", None) or {}
    if report:
        state = "passed" if report.get("ok") else f"failed with {report.get('errors', 0)} error(s)"
        lines.append(f"Deterministic checks: {state} over {report.get('checked', 0)} file(s).")
        for item in (report.get("issues") or [])[:6]:
            lines.append(f"- {item.get('file')}: {item.get('detail')}")
    for item in getattr(run, "file_summary", lambda: [])():
        lines.append(f"File {item['path']}: {item['lines']} lines.")
    criteria = list(getattr(run, "acceptance", None) or [])
    if criteria:
        lines.append(
            "Named capabilities: "
            + ", ".join(f"{item.get('label')}{'' if item.get('found') else ' (not found)'}" for item in criteria[:10])
        )
    if getattr(run, "browser", None):
        lines.append(f"Browser check: {'clean' if run.browser.get('ok') else 'found issues'}.")
    return "\n".join(lines) or "No measured facts available."


def write(run, goal: str) -> None:
    """Persist the goal beside the run, so a resume judges the same statement."""
    workspace = getattr(run, "workspace", None)
    if workspace is None:
        return
    fm = getattr(run, "fm", None)
    body = f"# Goal\n\n{goal}\n"
    try:
        if fm is not None:
            fm.write(FILENAME, body)
        else:
            from pathlib import Path

            (Path(workspace) / FILENAME).write_text(body, encoding="utf-8")
    except OSError:
        pass


def read(run) -> str:
    """The persisted goal, or a freshly derived one when the file is absent."""
    workspace = getattr(run, "workspace", None)
    if workspace is None:
        return derive(run)
    from pathlib import Path

    path = Path(workspace) / FILENAME
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return derive(run)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return derive(run)


def contract(criteria: list[dict] | None) -> str:
    """The goal block for a code prompt: what finished looks like, stated once."""
    return intent.contract(criteria or [])
