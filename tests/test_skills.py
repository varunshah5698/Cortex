"""Skills: reusable procedures advertised by name, read only on demand.

Every rule pinned here exists because its failure is silent. A skill whose `name:` does not
match its directory is dropped at load with no error; a skill nothing names is never opened;
a helper a skill invokes but does not ship is gone. None of those produce a broken run —
they produce a run that quietly never used the procedure it was given.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import skills  # noqa: E402

GOOD = """---
name: pdf-report
description: Build a page-cited PDF report from a source packet.
---

1. Read the packet part by part.
2. Cite the page number with every figure.
"""

TRIGGER = "When the task asks for a report, load the `pdf-report` skill."


def _tree(text: str = GOOD, name: str = "pdf-report", trigger: str = TRIGGER) -> dict[str, str]:
    return {
        f"skills/{name}/SKILL.md": text,
        "AGENTS.md": trigger,
    }


def test_a_skill_is_advertised_by_name_and_description_only() -> None:
    catalog = skills.catalog(_tree())

    assert "pdf-report" in catalog
    assert "page-cited PDF report" in catalog
    assert "Read the packet part by part" not in catalog, "the body is not advertised, only offered"


def test_opening_a_skill_returns_its_procedure_without_the_frontmatter() -> None:
    opened = skills.open_skill(_tree(), "pdf-report")

    assert "Read the packet part by part" in opened
    assert "description:" not in opened, "the frontmatter is metadata, not procedure"
    assert opened.startswith("Skill `pdf-report`")


def test_opening_an_unknown_skill_names_what_exists() -> None:
    with pytest.raises(LookupError) as refused:
        skills.open_skill(_tree(), "nope")

    assert "pdf-report" in str(refused.value), "the error lists the skills this run actually has"


def test_a_name_that_does_not_match_its_directory_is_an_error() -> None:
    """The load drops this skill silently, so the check is the only thing that says so."""
    text = GOOD.replace("name: pdf-report", "name: something-else")

    problems = skills.validate(_tree(text), TRIGGER)

    assert any("declares `name: something-else`" in problem for problem in problems)


def test_a_missing_description_is_an_error() -> None:
    text = """---
name: pdf-report
---

1. Do the work.
"""

    problems = skills.validate(_tree(text), TRIGGER)

    assert any("needs a `description:`" in problem for problem in problems)


def test_a_skill_nothing_names_is_an_error() -> None:
    """An untriggered skill is dead weight: the model never learns it exists."""
    problems = skills.validate(_tree(), "no mention of any skill here")

    assert any("never named in the always-on text" in problem for problem in problems)
    assert any("load the `pdf-report` skill" in problem for problem in problems), "the fix is spelled out"


def test_a_missing_frontmatter_block_is_an_error() -> None:
    problems = skills.validate(_tree("1. Just a procedure, no frontmatter."), TRIGGER)

    assert any("has no frontmatter block" in problem for problem in problems)


def test_a_helper_a_skill_invokes_must_ship_with_it() -> None:
    text = GOOD + "\nRun `python3 scripts/pack.py` first.\n"

    problems = skills.validate(_tree(text), TRIGGER)

    assert any("invokes `scripts/pack.py`" in problem for problem in problems)
    assert any("skills/pdf-report/scripts/pack.py" in problem for problem in problems), "names where it goes"


def test_a_shipped_helper_is_accepted() -> None:
    tree = _tree(GOOD + "\nRun `python3 scripts/pack.py` first.\n")
    tree["skills/pdf-report/scripts/pack.py"] = "print('packed')\n"

    assert skills.validate(tree, TRIGGER) == []


def test_a_tree_with_no_skills_advertises_nothing() -> None:
    assert skills.catalog({"index.html": "<html></html>"}) == ""
    assert skills.validate({"index.html": "<html></html>"}, "") == []


def test_the_census_counts_what_the_tree_has() -> None:
    tree = _tree()
    tree["skills/pdf-report/scripts/pack.py"] = "print('packed')\n"

    census = skills.census(tree)

    assert census == {"skills": 1, "described": 1, "skill_scripts": 1}


def test_the_verifier_reports_an_untriggered_skill_as_a_finding() -> None:
    """A silent drop at load has to become a failing check, or nothing ever notices."""
    from ai_harness.nodes import verify

    files = {
        "skills/pdf-report/SKILL.md": {"content": GOOD, "agent": "a", "done": True},
        "AGENTS.md": {"content": "no skill mentioned", "agent": "a", "done": True},
    }

    report = verify.check(files, list(files))

    assert report["ok"] is False
    assert any(item["code"] == "skill" for item in report["issues"])


def test_the_verifier_passes_a_well_formed_triggered_skill() -> None:
    from ai_harness.nodes import verify

    files = {
        "skills/pdf-report/SKILL.md": {"content": GOOD, "agent": "a", "done": True},
        "AGENTS.md": {"content": TRIGGER, "agent": "a", "done": True},
    }

    report = verify.check(files, list(files))

    assert not [item for item in report["issues"] if item["code"] == "skill"]
