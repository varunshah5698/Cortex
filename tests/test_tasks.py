"""The task graph: dependencies that are enforced, and a claim that is atomic.

The two claims worth testing are the ones a checklist cannot make: a blocked task is not
ready, and two coroutines cannot both claim the same task. Everything else is bookkeeping.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import WorkUnit  # noqa: E402
from ai_harness.modules import tasks  # noqa: E402
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
        WorkUnit(id="u3", title="Page", role="frontend", file="index.html", objective="renders", agent="c"),
    ]


def test_seed_writes_one_task_per_unit_in_plan_order(tmp_path: Path):
    run = FakeRun(tmp_path)

    seeded = tasks.seed(run, _units())

    assert [task.id for task in seeded] == ["u1", "u2", "u3"]
    assert (tmp_path / tasks.FILENAME).is_file()
    # No edges are invented: the plan does not state an order, so nothing is blocked.
    assert len(tasks.ready(run)) == 3


def test_a_blocked_task_is_not_ready_until_its_blocker_is_done(tmp_path: Path):
    run = FakeRun(tmp_path)
    tasks.seed(run, _units())
    tasks.depends(run, "u2", "u1")

    ready = [task.id for task in tasks.ready(run)]
    assert "u2" not in ready and "u1" in ready

    assert tasks.claim(run, "u2", "b") is None, "a blocked task must not be claimable"

    tasks.complete(run, "u1")
    assert "u2" in [task.id for task in tasks.ready(run)]
    claimed = tasks.claim(run, "u2", "b")
    assert claimed is not None and claimed.owner == "b"


def test_two_coroutines_cannot_claim_the_same_task(tmp_path: Path):
    run = FakeRun(tmp_path)
    tasks.seed(run, _units())

    async def race() -> list:
        return await asyncio.gather(
            asyncio.to_thread(tasks.claim, run, "u1", "a"),
            asyncio.to_thread(tasks.claim, run, "u1", "b"),
        )

    results = asyncio.run(race())
    winners = [item for item in results if item is not None]
    assert len(winners) == 1, results


def test_the_graph_survives_a_restart_because_it_is_a_file(tmp_path: Path):
    run = FakeRun(tmp_path)
    tasks.seed(run, _units())
    tasks.depends(run, "u3", "u2")
    tasks.claim(run, "u1", "a")

    # A new run object over the same workspace is what a resume looks like.
    resumed = FakeRun(tmp_path)
    loaded = tasks.load(resumed)

    assert [task.id for task in loaded] == ["u1", "u2", "u3"]
    assert loaded[0].status == tasks.IN_PROGRESS and loaded[0].owner == "a"
    assert loaded[2].blockedBy == ["u2"]


def test_a_blocked_task_records_why(tmp_path: Path):
    run = FakeRun(tmp_path)
    tasks.seed(run, _units())

    tasks.block(run, "u3", "waiting on the design")

    task = next(item for item in tasks.load(run) if item.id == "u3")
    assert task.status == tasks.BLOCKED
    assert "waiting on the design" in task.objective


def test_orient_names_what_is_ready_and_what_is_waiting(tmp_path: Path):
    run = FakeRun(tmp_path)
    tasks.seed(run, _units())
    tasks.depends(run, "u2", "u1")
    tasks.complete(run, "u1")

    text = tasks.orient(run)

    assert "1/3 done" in text
    assert "u2" in text and "u3" in text


def test_an_empty_graph_answers_nothing_rather_than_inventing_work(tmp_path: Path):
    run = FakeRun(tmp_path)

    assert tasks.load(run) == []
    assert tasks.ready(run) == []
    assert tasks.orient(run) == ""
    assert tasks.summary(run)["total"] == 0


def test_a_run_with_no_workspace_is_a_no_op(tmp_path: Path):
    class Bare:
        files: dict = {}

    assert tasks.seed(Bare(), _units()) == []
    assert tasks.ready(Bare()) == []
