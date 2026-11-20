"""A file has exactly one author.

A weak planner splits one page into "hero markup" and "footer markup" and names
`index.html` for both. Two agents then write the same path, the second write wins,
and the first author's work disappears without an error. The plan is merged so the
file keeps one owner and every objective survives.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import WorkUnit  # noqa: E402
from ai_harness.nodes.planning import _one_owner_per_file  # noqa: E402


def _unit(uid: str, title: str, path: str, objective: str) -> WorkUnit:
    return WorkUnit(id=uid, title=title, role="frontend", file=path, objective=objective)


def test_two_units_on_one_file_become_one_unit():
    units = [
        _unit("wu-1", "Hero Section Markup", "index.html", "Build the hero."),
        _unit("wu-2", "Product Grid Markup", "src/styles.css", "Write the styles."),
        _unit("wu-3", "Footer Markup", "index.html", "Build the footer."),
    ]

    merged = _one_owner_per_file(units)

    assert [unit.file for unit in merged] == ["index.html", "src/styles.css"]


def test_the_merged_unit_keeps_every_objective():
    units = [
        _unit("wu-1", "Hero Section Markup", "index.html", "Build the hero."),
        _unit("wu-3", "Footer Markup", "index.html", "Build the footer."),
    ]

    merged = _one_owner_per_file(units)

    assert len(merged) == 1
    assert "Build the hero." in merged[0].objective
    assert "Build the footer." in merged[0].objective


def test_distinct_files_are_left_alone():
    units = [
        _unit("wu-1", "Hero", "index.html", "Build the hero."),
        _unit("wu-2", "Styles", "src/styles.css", "Write the styles."),
        _unit("wu-3", "Script", "src/app.js", "Wire the grid."),
    ]

    merged = _one_owner_per_file(units)

    assert [unit.id for unit in merged] == ["wu-1", "wu-2", "wu-3"]


def test_a_unit_with_no_file_is_kept():
    units = [
        _unit("wu-1", "Hero", "index.html", "Build the hero."),
        _unit("wu-2", "Review pass", "", "Check the copy."),
    ]

    merged = _one_owner_per_file(units)

    assert [unit.id for unit in merged] == ["wu-1", "wu-2"]
