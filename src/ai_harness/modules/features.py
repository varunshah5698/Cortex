"""`feature_list.json`: scope as a machine-readable gate, not a checklist.

`progress.py` writes `todo.md`, one line per planned file, ticked when the file lands.
That answers "did the author write the file". It does not answer the question the course
insists on (Lecture 08): *is this feature actually working, and is there evidence?* A
ticked line and a passing feature are different claims, and only one of them can be
checked.

So this module keeps a feature list with the three properties that make it a harness
primitive rather than a document:

* **One active feature.** `single_active_feature` is enforced, not suggested: `activate()`
  refuses a second feature while one is in progress. A model that starts three things
  finishes none of them, and the file is where that rule can actually be applied.
* **Passing requires evidence.** A feature moves to `passing` only through `pass()`, and
  `pass()` refuses an empty evidence list. There is no path from `not_started` to
  `passing` that skips a recorded fact. This is the pass gate from the course, as code.
* **The file is the record.** Every mutation rewrites `feature_list.json` in the run's own
  workspace through `FileManager`, so a restart resumes the same scope and a reviewer can
  read what was claimed without replaying a transcript.

The list is seeded from the plan's work units, so it costs nothing on a run that never
uses it. `orient()` returns the block a prompt re-injects, and `summary()` feeds the
metrics and the receipt.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

FILENAME = "feature_list.json"

NOT_STARTED = "not_started"
IN_PROGRESS = "in_progress"
BLOCKED = "blocked"
PASSING = "passing"
STATUSES = (NOT_STARTED, IN_PROGRESS, BLOCKED, PASSING)

# The rules the file states about itself. They are written into the JSON so a reader (human
# or model) sees the contract next to the data it governs, and they are enforced below.
RULES = {
    "single_active_feature": True,
    "passing_requires_evidence": True,
    "do_not_skip_verification": True,
}

LEGEND = {
    NOT_STARTED: "Work has not begun.",
    IN_PROGRESS: "The feature is the current active task.",
    BLOCKED: "Work cannot continue until a documented blocker is resolved.",
    PASSING: "Required verification has passed and evidence is recorded.",
}


class GateRefused(RuntimeError):
    """A state change the feature-list contract does not allow."""


@dataclass
class Feature:
    id: str
    title: str
    priority: int = 0
    area: str = ""
    user_visible_behavior: str = ""
    status: str = NOT_STARTED
    verification: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    notes: str = ""
    file: str = ""

    def public(self) -> dict:
        return asdict(self)


def _path(run):
    """The list's path, or None for a run object with no workspace (a focused test)."""
    workspace = getattr(run, "workspace", None)
    if workspace is None:
        return None
    from pathlib import Path

    return Path(workspace) / FILENAME


def _save(run, features: list[Feature]) -> None:
    path = _path(run)
    if path is None:
        return
    body = json.dumps(
        {
            "project": str(getattr(run, "prompt", "") or "")[:120],
            "rules": RULES,
            "status_legend": LEGEND,
            "features": [feature.public() for feature in features],
        },
        indent=2,
    )
    fm = getattr(run, "fm", None)
    try:
        if fm is not None:
            fm.write(FILENAME, body)
        else:
            path.write_text(body, encoding="utf-8")
    except OSError:
        pass


def load(run) -> list[Feature]:
    """The list from disk, or an empty list when this run never seeded one."""
    path = _path(run)
    if path is None:
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("features") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    features: list[Feature] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        feature_id = str(entry.get("id") or "").strip()
        if not feature_id:
            continue
        status = str(entry.get("status") or NOT_STARTED)
        features.append(
            Feature(
                id=feature_id,
                title=str(entry.get("title") or feature_id),
                priority=int(entry.get("priority") or 0),
                area=str(entry.get("area") or ""),
                user_visible_behavior=str(entry.get("user_visible_behavior") or ""),
                status=status if status in STATUSES else NOT_STARTED,
                verification=[str(item) for item in (entry.get("verification") or []) if str(item).strip()],
                evidence=[str(item) for item in (entry.get("evidence") or []) if str(item).strip()],
                notes=str(entry.get("notes") or ""),
                file=str(entry.get("file") or ""),
            )
        )
    return features


def seed(run, units: list) -> list[Feature]:
    """One feature per planned work unit, in plan order, none started.

    The verification steps are not invented: a unit that names a file gets the one check
    the harness can actually run on it (the verifier), and the rest is left to the caller.
    A feature list full of plausible-sounding steps nobody executes is worse than a short
    honest one.
    """
    features = [
        Feature(
            id=str(getattr(unit, "id", "") or f"f{index + 1}"),
            title=str(getattr(unit, "title", "") or getattr(unit, "file", "") or "feature"),
            priority=index + 1,
            area=str(getattr(unit, "role", "") or ""),
            user_visible_behavior=str(getattr(unit, "objective", "") or "")[:400],
            file=str(getattr(unit, "file", "") or ""),
            verification=["the deterministic verifier reports no error for this file"],
        )
        for index, unit in enumerate(units or [])
    ]
    _save(run, features)
    return features


def activate(run, feature_id: str) -> Feature:
    """Make `feature_id` the one active feature.

    Refuses when another feature is already in progress: that is the whole point of
    `single_active_feature`, and a rule that is only written down is not a rule.
    """
    features = load(run)
    target = next((feature for feature in features if feature.id == feature_id), None)
    if target is None:
        raise GateRefused(f"no feature {feature_id!r} in {FILENAME}")
    if target.status == PASSING:
        raise GateRefused(f"feature {feature_id!r} already passes; nothing to activate")
    active = [feature.id for feature in features if feature.status == IN_PROGRESS and feature.id != feature_id]
    if active:
        raise GateRefused(
            f"feature {feature_id!r} cannot start while {', '.join(active)} is in progress (single_active_feature)"
        )
    target.status = IN_PROGRESS
    _save(run, features)
    return target


def pass_feature(run, feature_id: str, evidence: list[str]) -> Feature:
    """Move a feature to `passing`. Refuses without evidence.

    This is the pass gate: the only door into `passing`, and it does not open on a claim.
    """
    proof = [str(item).strip() for item in (evidence or []) if str(item).strip()]
    if not proof:
        raise GateRefused(f"feature {feature_id!r} cannot pass without evidence (passing_requires_evidence)")
    features = load(run)
    target = next((feature for feature in features if feature.id == feature_id), None)
    if target is None:
        raise GateRefused(f"no feature {feature_id!r} in {FILENAME}")
    target.status = PASSING
    target.evidence = [*target.evidence, *proof]
    _save(run, features)
    return target


def block(run, feature_id: str, reason: str = "") -> list[Feature]:
    """Park a feature, keeping the reason where the next reader will find it."""
    features = load(run)
    target = next((feature for feature in features if feature.id == feature_id), None)
    if target is not None:
        target.status = BLOCKED
        if reason:
            target.notes = f"{target.notes} [blocked: {reason}]".strip()
        _save(run, features)
    return features


def summary(run) -> dict:
    """Counts, the active feature, and what is still unproven."""
    features = load(run)
    counts = {status: 0 for status in STATUSES}
    for feature in features:
        counts[feature.status] = counts.get(feature.status, 0) + 1
    active = next((feature.id for feature in features if feature.status == IN_PROGRESS), "")
    unproven = [feature.id for feature in features if feature.status == PASSING and not feature.evidence]
    return {
        "total": len(features),
        "counts": counts,
        "active": active,
        "unproven": unproven,
        "passing": [feature.id for feature in features if feature.status == PASSING],
    }


def orient(run, limit: int = 10) -> str:
    """The recitation block for a prompt: the active feature, and what is not passing."""
    features = load(run)
    if not features:
        return ""
    passing = [feature for feature in features if feature.status == PASSING]
    head = f"FEATURES ({FILENAME}): {len(passing)}/{len(features)} passing."
    active = next((feature for feature in features if feature.status == IN_PROGRESS), None)
    if active is not None:
        head += f" Active: {active.id} — {active.title}. Finish it before starting another."
    open_features = [feature for feature in features if feature.status != PASSING]
    if not open_features:
        return head + " Every feature passes with evidence."
    lines = [head]
    for feature in open_features[:limit]:
        lines.append(f"- {feature.id} · {feature.title} · {feature.status}")
    if len(open_features) > limit:
        lines.append(f"… and {len(open_features) - limit} more.")
    return "\n".join(lines)
