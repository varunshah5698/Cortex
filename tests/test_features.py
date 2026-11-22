"""The feature list: a scope gate that refuses, not a document that describes.

The three claims worth testing are the ones that make this a harness primitive rather than
a checklist: one active feature at a time, `passing` unreachable without evidence, and the
file on disk being the record a restart reads. Everything else is bookkeeping.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import WorkUnit  # noqa: E402
from ai_harness.modules import features  # noqa: E402
from ai_harness.tools.file_manager import FileManager  # noqa: E402


class FakeRun:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.fm = FileManager(workspace)
        self.files: dict[str, dict] = {}
        self.prompt = "Build a dashboard"


def _units() -> list[WorkUnit]:
    return [
        WorkUnit(id="u1", title="Schema", role="backend", file="schema.py", objective="tables", agent="a"),
        WorkUnit(id="u2", title="Client", role="backend", file="client.py", objective="calls", agent="b"),
    ]


def test_seed_writes_one_feature_per_unit_and_none_start(tmp_path: Path):
    run = FakeRun(tmp_path)

    seeded = features.seed(run, _units())

    assert [feature.id for feature in seeded] == ["u1", "u2"]
    assert all(feature.status == features.NOT_STARTED for feature in seeded)
    assert (tmp_path / features.FILENAME).is_file()
    # The rules travel with the data, so a reader sees the contract beside what it governs.
    raw = json.loads((tmp_path / features.FILENAME).read_text(encoding="utf-8"))
    assert raw["rules"]["single_active_feature"] is True
    assert raw["rules"]["passing_requires_evidence"] is True


def test_activate_refuses_a_second_active_feature(tmp_path: Path):
    run = FakeRun(tmp_path)
    features.seed(run, _units())

    features.activate(run, "u1")

    with pytest.raises(features.GateRefused) as excinfo:
        features.activate(run, "u2")
    assert "single_active_feature" in str(excinfo.value)
    # The refusal must not have changed anything on disk.
    assert features.summary(run)["active"] == "u1"


def test_activate_is_allowed_again_once_the_first_feature_passes(tmp_path: Path):
    run = FakeRun(tmp_path)
    features.seed(run, _units())
    features.activate(run, "u1")
    features.pass_feature(run, "u1", ["verifier: no error for schema.py"])

    features.activate(run, "u2")

    assert features.summary(run)["active"] == "u2"


def test_passing_without_evidence_is_refused(tmp_path: Path):
    run = FakeRun(tmp_path)
    features.seed(run, _units())
    features.activate(run, "u1")

    with pytest.raises(features.GateRefused) as excinfo:
        features.pass_feature(run, "u1", [])

    assert "passing_requires_evidence" in str(excinfo.value)
    assert features.load(run)[0].status == features.IN_PROGRESS


def test_blank_evidence_is_the_same_as_no_evidence(tmp_path: Path):
    """A whitespace string is a claim with nothing in it. It must not open the gate."""
    run = FakeRun(tmp_path)
    features.seed(run, _units())

    with pytest.raises(features.GateRefused):
        features.pass_feature(run, "u1", ["   ", ""])


def test_passing_records_the_evidence_it_was_given(tmp_path: Path):
    run = FakeRun(tmp_path)
    features.seed(run, _units())

    features.pass_feature(run, "u1", ["verifier: no error for schema.py"])

    feature = features.load(run)[0]
    assert feature.status == features.PASSING
    assert feature.evidence == ["verifier: no error for schema.py"]
    assert features.summary(run)["unproven"] == []


def test_evidence_accumulates_across_passes(tmp_path: Path):
    run = FakeRun(tmp_path)
    features.seed(run, _units())

    features.pass_feature(run, "u1", ["first check"])
    features.pass_feature(run, "u1", ["second check"])

    assert features.load(run)[0].evidence == ["first check", "second check"]


def test_a_passing_feature_cannot_be_reactivated(tmp_path: Path):
    run = FakeRun(tmp_path)
    features.seed(run, _units())
    features.pass_feature(run, "u1", ["evidence"])

    with pytest.raises(features.GateRefused):
        features.activate(run, "u1")


def test_an_unknown_feature_is_refused_rather_than_created(tmp_path: Path):
    run = FakeRun(tmp_path)
    features.seed(run, _units())

    with pytest.raises(features.GateRefused):
        features.activate(run, "nope")
    with pytest.raises(features.GateRefused):
        features.pass_feature(run, "nope", ["evidence"])


def test_the_file_on_disk_is_what_a_restart_reads(tmp_path: Path):
    """A second reader with no memory of the first must see the same state."""
    run = FakeRun(tmp_path)
    features.seed(run, _units())
    features.activate(run, "u1")
    features.pass_feature(run, "u1", ["verifier: clean"])

    fresh = FakeRun(tmp_path)
    loaded = features.load(fresh)

    assert [feature.id for feature in loaded] == ["u1", "u2"]
    assert loaded[0].status == features.PASSING
    assert loaded[0].evidence == ["verifier: clean"]


def test_a_corrupt_file_reads_as_empty_rather_than_raising(tmp_path: Path):
    """A feature list is context. Context that cannot be read must not stop work."""
    run = FakeRun(tmp_path)
    (tmp_path / features.FILENAME).write_text("{not json", encoding="utf-8")

    assert features.load(run) == []
    assert features.summary(run)["total"] == 0
    assert features.orient(run) == ""


def test_an_unknown_status_is_read_as_not_started(tmp_path: Path):
    run = FakeRun(tmp_path)
    (tmp_path / features.FILENAME).write_text(
        json.dumps({"features": [{"id": "u1", "title": "T", "status": "shipped"}]}),
        encoding="utf-8",
    )

    assert features.load(run)[0].status == features.NOT_STARTED


def test_block_keeps_the_reason_where_the_next_reader_finds_it(tmp_path: Path):
    run = FakeRun(tmp_path)
    features.seed(run, _units())

    features.block(run, "u1", "waiting on the schema")

    feature = features.load(run)[0]
    assert feature.status == features.BLOCKED
    assert "waiting on the schema" in feature.notes


def test_orient_names_the_active_feature_and_tells_the_model_to_finish_it(tmp_path: Path):
    run = FakeRun(tmp_path)
    features.seed(run, _units())
    features.activate(run, "u1")

    text = features.orient(run)

    assert "0/2 passing" in text
    assert "u1" in text
    assert "Finish it before starting another" in text


def test_orient_on_a_fully_passing_list_says_so(tmp_path: Path):
    run = FakeRun(tmp_path)
    features.seed(run, _units())
    features.pass_feature(run, "u1", ["a"])
    features.pass_feature(run, "u2", ["b"])

    assert "Every feature passes with evidence" in features.orient(run)


def test_orient_is_empty_with_no_list_so_a_prompt_gains_nothing(tmp_path: Path):
    run = FakeRun(tmp_path)

    assert features.orient(run) == ""


def test_a_run_with_no_workspace_is_inert(tmp_path: Path):
    """A focused test object has no workspace; every call must be a no-op, not a crash."""

    class Bare:
        prompt = "x"

    assert features.load(Bare()) == []
    assert features.orient(Bare()) == ""
    assert features.summary(Bare())["total"] == 0
