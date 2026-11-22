"""Run the golden cases through the real pipeline and score what came out.

Scripted mode (the default) stubs the model only: planning, scaffolding, execution, the
verifier, review and synthesis all run for real, offline, for free. Live mode sends the
same prompts to the configured providers, which is the mode that can answer "did my
prompt change make builds better".

A run is scored on facts, not opinion: does it finish, does the verifier reach the
expected verdict, and are the files that should have content non-empty. The prompt
fingerprints are recorded with every result so a score change can be read against the
wording that produced it.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import ai_harness.core.state as state_mod

from ..config.settings import KEYS_FILE, ROOT, ROSTER_FILE
from ..core.orchestrator import Orchestrator
from ..core.state import RunState
from ..models import Specialist
from ..modules import budget as pricing
from ..modules import failures, llm, memory, prompts, runner
from ..modules.roster import RosterStore
from ..nodes import verify
from ..tools import web_search
from ..utils import logger
from . import build as build_tier
from . import judge as judge_tier
from .cases import CASES, Case
from .script import Scripted

RESULTS_DIR = Path(ROOT) / "evals" / "results"
BASELINE = Path(ROOT) / "evals" / "baseline.json"

# The stamp of the suite that just ran, so `save` names its aggregate after the journal
# that recorded the same run instead of minting a second timestamp a second later.
LAST_STAMP: str | None = None


def journal_path(stamp: str) -> Path:
    """The append-only record of one suite, beside the aggregate it becomes."""
    return RESULTS_DIR / f"{stamp}.jsonl"


def append_journal(path: Path, result: dict) -> None:
    """One line per finished case, flushed and fsynced before the next case starts.

    A suite killed at case five keeps the four that already finished. That is the whole
    point: the aggregate written by `save` is only reachable once every case is in, so
    without this a crash costs the entire run — and in live mode, the entire spend.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_journal(path: Path) -> list[dict]:
    """Every complete line of a journal, in order.

    A torn final line is the crash itself, not a result: it is dropped rather than
    raised, because the case it belonged to never finished and has nothing to report.
    """
    results: list[dict] = []
    if not path.is_file():
        return results
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return results


def missing_cases(results: list[dict]) -> list[str]:
    """Golden cases this journal never recorded, in suite order.

    A recovered journal is a *prefix* of a suite, not the suite. Without this the table
    for a suite killed at case five is indistinguishable from a clean five-case run: every
    row says `ok`, the exit status is 0, and the two cases that never ran are simply
    absent. A partial recovery has to say so out loud.
    """
    recorded = {str(item.get("case")) for item in results}
    return [case.name for case in CASES if case.name not in recorded]


def journal_matches(results: list[dict], only: list[str] | None = None) -> list[str]:
    """The cases a `--case` filter asked for that the journal does not hold."""
    if not only:
        return []
    recorded = {str(item.get("case")) for item in results}
    return [name for name in only if name not in recorded]


def scripted_team() -> list[Specialist]:
    """Four scripted specialists. A non-loopback base keeps the full pipeline (research
    and contracts included) in the eval; `Scripted` answers before any socket opens."""
    base = "http://eval.invalid/v1"
    return [
        Specialist(id="eval-planner", name="Atlas", role="planner", kind="openai", baseUrl=base, model="scripted"),
        Specialist(id="eval-front", name="Vega", role="frontend", kind="openai", baseUrl=base, model="scripted"),
        Specialist(id="eval-back", name="Orion", role="backend", kind="openai", baseUrl=base, model="scripted"),
        Specialist(id="eval-review", name="Lyra", role="reviewer", kind="openai", baseUrl=base, model="scripted"),
    ]


def live_team() -> list[Specialist]:
    """The configured roster, with at least a planner and a reviewer."""
    ready = [spec for spec in RosterStore(ROSTER_FILE, KEYS_FILE).load() if spec.ready]
    if not ready:
        raise SystemExit(
            "no configured specialist in the roster — save keys in the workshop first, or run without --live"
        )
    planner = next((spec for spec in ready if spec.role == "planner"), ready[0])
    reviewer = next((spec for spec in ready if spec.role == "reviewer"), planner)
    builders = [spec for spec in ready if spec.role in ("frontend", "backend", "general")]
    return [planner, *(builders or [planner]), reviewer]


def must_have(case: Case) -> list[str]:
    """Files the case intends to have content; an empty one is the point of a case."""
    return [path for path in case.files if case.code.get(path, "").strip()]


def flagged(case: Case, run: RunState) -> bool:
    """True when every code the case requires was actually reported by a verify pass."""
    if not case.must_flag:
        return True
    seen = {
        str(item.get("code"))
        for event in run.events
        if event.get("type") == "verify"
        for item in event.get("issues") or []
    }
    return case.must_flag <= seen


def score(case: Case, run: RunState) -> dict:
    report = run.verification or {}
    errors = [event for event in run.events if event.get("type") == "error"]
    checks = {
        # A run that passes verification must be reported done; one that does not must
        # not be. This is the terminal-verdict gate, not merely "the task returned".
        "reported_done": bool(run.done) == case.expect_ok,
        "verification": bool(report.get("ok")) == case.expect_ok,
        "files": all((run.files.get(path) or {}).get("content", "").strip() for path in must_have(case)),
        "clean": not errors if case.expect_ok else True,
        # Independent of the final verdict: a repaired run still had to be caught first.
        "flagged": flagged(case, run),
    }
    return {
        "checks": checks,
        "passed": sum(1 for ok in checks.values() if ok),
        "total": len(checks),
        "verification_ok": bool(report.get("ok")),
        "errors": [str(event.get("message"))[:160] for event in errors][:3],
    }


def unpriced_message(missing: list[dict]) -> str:
    """What the suite says instead of running on a guessed price."""
    lines = ["these models have no price row, so HARNESS_MAX_USD would be a guess:"]
    for item in missing:
        where = item["baseUrl"] or "no base URL"
        who = ", ".join(item["ids"]) or "the roster"
        lines.append(f"  {item['model'] or '(no model)'} · {where} · {who}")
    lines.append(
        "add a row to modules/budget.py TABLE, or set "
        "HARNESS_PRICES='{\"<model>\": [<input>, <output>]}' in USD per 1M tokens."
    )
    return "\n".join(lines)


async def _stop_preview(run: RunState) -> None:
    """Close the preview a finished case started; an eval never needs it after scoring."""
    try:
        await runner.stop(run.run_id)
    finally:
        runner.SERVICES.pop(run.run_id, None)


async def run_case(
    case: Case,
    live: bool = False,
    build: bool = False,
    judge: bool = False,
    team: list[Specialist] | None = None,
) -> dict:
    run = RunState(
        run_id=uuid.uuid4().hex[:12],
        prompt=case.prompt,
        specialists=team if team is not None else (live_team() if live else scripted_team()),
        depth="quick" if live else "deep",
        kind="eval",
    )
    started = time.perf_counter()

    if live:
        try:
            await Orchestrator(run).run_all()
        finally:
            await _stop_preview(run)
        calls = None
    else:
        scripted = Scripted(case)
        original_call, original_stream = llm.call, llm.stream
        original_research, original_reachable = web_search.research, llm.reachable
        original_tools = llm.call_tools

        async def no_research(_queries, log=None):  # noqa: ANN001 - matches web_search.research
            return [], []

        async def reachable(_spec):  # noqa: ANN001 - the scripted provider has no endpoint
            return True, ""

        async def no_native(*_args, **_kwargs):  # noqa: ANN002, ANN003 - offline suite
            raise llm.NativeToolsUnsupported("the scripted suite uses the text protocol")

        llm.call, llm.stream, llm.reachable, llm.call_tools = (
            scripted.call,
            scripted.stream,
            reachable,
            no_native,
        )
        web_search.research = no_research
        try:
            await Orchestrator(run).run_all()
        finally:
            llm.call, llm.stream, llm.reachable, llm.call_tools = (
                original_call,
                original_stream,
                original_reachable,
                original_tools,
            )
            web_search.research = original_research
            await _stop_preview(run)
        calls = scripted.calls

    result = score(case, run)
    # The build tier is a second sensor over the same tree: the facts above can all be
    # right and the thing still not compile. It writes the case's own files, so a
    # build failure is about the case and never about this run's workspace.
    tier = await build_tier.check(case) if build else None
    # Advisory only: the score is recorded and printed, and `__main__` never reads it for
    # the exit status. The deterministic checks above, and the build tier, are the gate.
    verdict = await judge_tier.score(case, run, judge_tier.judge_agent(run)) if judge else None
    return {
        "case": case.name,
        "note": case.note,
        "expect_ok": case.expect_ok,
        "live": live,
        "calls": calls,
        "ms": int((time.perf_counter() - started) * 1000),
        "prompt_fingerprints": prompts.fingerprints(),
        "always_on": prompts.always_on(),
        **result,
        "build": tier,
        "judge": verdict,
    }


def replay_regressions(only: list[str] | None = None) -> list[dict]:
    """Promoted failures, replayed through the verifier with no model involved.

    A case promoted from a blocked run (`modules/failures.promote`) is the exact tree that
    once slipped through, so the only question is whether it is still caught. That is a
    deterministic check: one verifier pass, no provider, no spend.
    """
    results: list[dict] = []
    for entry in failures.load_regressions():
        name = str(entry.get("name") or "regression")
        if only and name not in only:
            continue
        files = {
            str(path): {"agent": "", "content": str(body), "done": True}
            for path, body in (entry.get("files") or {}).items()
        }
        report = verify.check(files, list(files))
        caught = not bool(report.get("ok"))
        results.append(
            {
                "case": name,
                "note": entry.get("note") or "promoted regression",
                "expect_ok": False,
                "live": False,
                "calls": 0,
                "ms": 0,
                "checks": {
                    "reported_done": caught,
                    "verification": caught,
                    "files": True,
                    "clean": True,
                    "flagged": True,
                },
                "passed": 4 if caught else 0,
                "total": 4,
                "verification_ok": bool(report.get("ok")),
                "errors": [f"{item['file']}: {item['detail']}" for item in report.get("issues") or []][:3],
                "prompt_fingerprints": prompts.fingerprints(),
                "always_on": prompts.always_on(),
                "build": None,
                "judge": None,
                "replay": True,
            }
        )
    return results


async def run_suite(
    live: bool = False, only: list[str] | None = None, build: bool = False, judge: bool = False
) -> list[dict]:
    cases = [case for case in CASES if not only or case.name in only]
    replays = replay_regressions(only)
    if not cases and not replays:
        raise SystemExit(f"no such case: {', '.join(only or [])}")

    # Before a single call: every model this suite would spend on must have a price row.
    # A dollar cap computed from the default is not a cap, so this refuses rather than
    # reporting a run that "cost $0.42" when nobody knows what it cost.
    team = live_team() if live else scripted_team()
    missing = pricing.unpriced(team)
    if missing:
        raise SystemExit(unpriced_message(missing))

    original_root = state_mod.RUNS_ROOT
    original_pages = (failures.LOG, failures.REGRESSIONS, memory.PATH)
    original_logs = logger.LOGS_ROOT
    sandbox = Path(ROOT) / "evals" / ".runs"
    sandbox.mkdir(parents=True, exist_ok=True)
    state_mod.RUNS_ROOT = sandbox
    # An eval suite deliberately runs cases that must fail, so it would otherwise append a
    # row to the project's failures.md and a case to its regressions on every run. Its
    # bookkeeping belongs in its own sandbox — including the event log, which is what the
    # build room reads: seven cases that are supposed to fail are not seven product
    # failures.
    failures.LOG = sandbox / "failures.md"
    failures.REGRESSIONS = sandbox / "regressions.jsonl"
    memory.PATH = sandbox / "memory.md"
    logger.LOGS_ROOT = sandbox / "logs"

    # The journal is written as each case lands, not after the last one. A suite that is
    # interrupted keeps everything it already paid for, and `--from-journal` can re-read
    # those results without asking a provider anything twice.
    global LAST_STAMP
    LAST_STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    journal = journal_path(LAST_STAMP)

    results: list[dict] = []
    try:
        for case in cases:
            result = await run_case(case, live=live, build=build, judge=judge, team=team)
            results.append(result)
            append_journal(journal, result)
        for replay in replays:
            results.append(replay)
            append_journal(journal, replay)
    finally:
        state_mod.RUNS_ROOT = original_root
        failures.LOG, failures.REGRESSIONS, memory.PATH = original_pages
        logger.LOGS_ROOT = original_logs
    return results


def latest_journal() -> str | None:
    """The stamp of the most recent suite that wrote a journal, or None.

    Lives here rather than in the CLI so the path is resolved against this module's
    `RESULTS_DIR` at call time: a caller that redirects the directory (a test, a sandbox)
    gets its own journals instead of an import-time copy of the real one.
    """
    found = sorted(path.stem for path in RESULTS_DIR.glob("*.jsonl"))
    return found[-1] if found else None


def save(results: list[dict]) -> Path:
    """The aggregate for a suite, named after the journal that recorded it.

    The stamp comes from `run_suite` (or from the journal being re-read), so the aggregate
    and its journal share one name and a recovered suite does not mint a second one.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = LAST_STAMP or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"{stamp}.json"
    path.write_text(json.dumps({"created": stamp, "results": results}, indent=2), encoding="utf-8")
    return path


def load_baseline() -> dict[str, int]:
    if not BASELINE.is_file():
        return {}
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    cases = data.get("cases") if isinstance(data, dict) else None
    return {str(name): int(value) for name, value in (cases or {}).items()}


def write_baseline(results: list[dict]) -> Path:
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "cases": {item["case"]: item["passed"] for item in results},
    }
    BASELINE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return BASELINE
