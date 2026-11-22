"""Focused checks for the harness: the sandbox must never be escaped, the money must
never be invisible, and a broken build must never pass verification."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.agents import executor  # noqa: E402
from ai_harness.agents import planner as planner_agent  # noqa: E402
from ai_harness.core import orchestrator as orch  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist, WorkUnit  # noqa: E402
from ai_harness.modules import (  # noqa: E402
    budget,
    llm,
    prompts,
    runner,  # noqa: E402
    runtime,
)
from ai_harness.modules.budget import Budget, BudgetExceeded  # noqa: E402
from ai_harness.modules.cache import CACHE, ResponseCache  # noqa: E402
from ai_harness.modules.llm import ProviderError  # noqa: E402
from ai_harness.modules.retry import _delay, is_retryable, with_retries  # noqa: E402
from ai_harness.nodes import (
    execution,  # noqa: E402
    verify,  # noqa: E402
)
from ai_harness.nodes import review as review_node  # noqa: E402
from ai_harness.tools.file_manager import FileManager  # noqa: E402
from ai_harness.utils.validators import UnsafePath, safe_relative  # noqa: E402


def spec(model: str = "gpt-4o-mini", base: str = "https://api.openai.com/v1", kind: str = "openai") -> Specialist:
    return Specialist(id="a1", name="Ada", model=model, baseUrl=base, kind=kind)


# ---------- sandbox ----------


def test_scaffold_creates_empty_files(tmp_path: Path) -> None:
    fm = FileManager(tmp_path)
    created = fm.scaffold(["index.html", "assets/styles.css", "app.js"])
    assert sorted(created) == ["app.js", "assets/styles.css", "index.html"]
    for path in created:
        target = tmp_path / path
        assert target.exists() and target.read_text() == ""


def test_a_symlinked_workspace_still_scaffolds(tmp_path: Path) -> None:
    """`/tmp` is a symlink on macOS; resolved targets must stay relative to the root."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    fm = FileManager(link)
    assert fm.scaffold(["src/main.tsx"]) == ["src/main.tsx"]
    assert (real / "src/main.tsx").exists()


def test_write_and_read_round_trip(tmp_path: Path) -> None:
    fm = FileManager(tmp_path)
    lines = fm.write("src/app.js", "const a = 1;\nconst b = 2;\n")
    assert lines == 3
    assert "const b" in fm.read("src/app.js")
    assert [item["path"] for item in fm.tree()] == ["src/app.js"]


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    fm = FileManager(tmp_path)
    for bad in ("../outside.txt", "/etc/passwd"):
        try:
            safe_relative(bad, tmp_path)
        except UnsafePath:
            continue
        raise AssertionError(f"{bad} should have been rejected")
    assert fm.scaffold(["../escape.txt"]) == []
    assert not (tmp_path.parent / "escape.txt").exists()


# ---------- cost ----------


def test_local_models_are_free(tmp_path: Path) -> None:
    local = spec("llama3.1:8b", "http://localhost:11434/v1", "ollama")
    assert budget.estimate(local, 50_000, 50_000) == 0.0


def test_price_table_matches_longest_model_name(tmp_path: Path) -> None:
    mini = budget.price_for(spec("gpt-4o-mini"))
    full = budget.price_for(spec("gpt-4o"))
    assert mini == (0.15, 0.60)
    assert full == (2.50, 10.00)
    assert budget.estimate(spec("gpt-4o-mini"), 1_000_000, 0) == 0.15


def test_unknown_model_falls_back_instead_of_crashing(tmp_path: Path) -> None:
    price = budget.price_for(spec("some-unlisted-model"))
    assert price == (budget.DEFAULT_INPUT, budget.DEFAULT_OUTPUT)


# ---------- cache ----------


def test_cache_serves_an_identical_request_once(tmp_path: Path) -> None:
    cache = ResponseCache(max_entries=4)
    one = spec()
    messages = [{"role": "user", "content": "plan this"}]
    assert cache.get(one, messages, 0.0) is None
    cache.set(one, messages, 0.0, {"text": "planned"})
    assert cache.get(one, messages, 0.0)["text"] == "planned"
    assert cache.stats()["hits"] == 1 and cache.stats()["misses"] == 1


def test_cache_keys_include_the_provider(tmp_path: Path) -> None:
    cache = ResponseCache(max_entries=4)
    messages = [{"role": "user", "content": "hi"}]
    cache.set(spec(base="https://a.example/v1"), messages, 0.0, {"text": "from a"})
    assert cache.get(spec(base="https://b.example/v1"), messages, 0.0) is None


def test_cache_keys_include_temperature(tmp_path: Path) -> None:
    """An entry recorded at one sampling setting must never satisfy another."""
    cache = ResponseCache(max_entries=4)
    messages = [{"role": "user", "content": "hi"}]
    cache.set(spec(), messages, 0.0, {"text": "cold"})
    assert cache.get(spec(), messages, 0.35) is None
    assert cache.get(spec(), messages, 0.0)["text"] == "cold"


def test_cache_refuses_non_deterministic_calls(tmp_path: Path) -> None:
    original_enabled, original_temp = llm.CACHE_ENABLED, llm.TEMPERATURE
    try:
        llm.CACHE_ENABLED = True
        llm.TEMPERATURE = 0.35
        assert llm.cacheable() is False, "a sampled request must never be cached"
        llm.TEMPERATURE = 0.0
        assert llm.cacheable() is True
        llm.CACHE_ENABLED = False
        assert llm.cacheable() is False, "the cache is opt-in"
    finally:
        llm.CACHE_ENABLED, llm.TEMPERATURE = original_enabled, original_temp


def test_cache_evicts_least_recently_used(tmp_path: Path) -> None:
    cache = ResponseCache(max_entries=1)
    first = [{"role": "user", "content": "first"}]
    second = [{"role": "user", "content": "second"}]
    cache.set(spec(), first, 0.0, {"text": "1"})
    cache.set(spec(), second, 0.0, {"text": "2"})
    assert cache.get(spec(), first, 0.0) is None
    assert cache.get(spec(), second, 0.0)["text"] == "2"


# ---------- budget ----------


def test_budget_blocks_a_run_once_exhausted(tmp_path: Path) -> None:
    budget = Budget(max_tokens=100, max_usd=0.0)
    budget.spend(60, 0.01)
    budget.check()
    budget.spend(50, 0.01)
    try:
        budget.check()
    except BudgetExceeded:
        pass
    else:
        raise AssertionError("budget should refuse a run past its token limit")
    assert budget.remaining_tokens == 0


def test_budget_caps_provider_calls(tmp_path: Path) -> None:
    budget = Budget(max_calls=2)
    budget.spend(1, 0.0)
    budget.check()
    budget.spend(1, 0.0)
    try:
        budget.check()
    except BudgetExceeded:
        return
    raise AssertionError("a call budget of 2 must refuse the third request")


def test_zero_budget_means_unlimited(tmp_path: Path) -> None:
    budget = Budget(max_tokens=0, max_usd=0.0)
    budget.spend(10_000_000, 250.0)
    budget.check()
    assert budget.remaining_usd is None


# ---------- retry ----------


def test_retry_classification_tells_weather_from_credentials(tmp_path: Path) -> None:
    # HTTP failures carry a status, which decides before any message hint
    assert is_retryable(ProviderError("Ada: HTTP 503 upstream", status=503))
    assert is_retryable(ProviderError("Ada: HTTP 429 rate limit", status=429))
    assert is_retryable(ProviderError("Ada: HTTP 402 free limit", status=402, retry_after=32.0))
    assert not is_retryable(ProviderError("Ada: HTTP 402 payment required", status=402))
    assert not is_retryable(ProviderError("Ada: HTTP 401 — check the API key", status=401))
    assert not is_retryable(ProviderError("Ada: HTTP 404 model missing", status=404))
    assert not is_retryable(ProviderError("Ada: HTTP 422 unprocessable", status=422))
    # no status: transport failures and errors streamed mid-response
    assert is_retryable(ProviderError("Ada: ReadTimeout('read timed out')"))
    assert is_retryable(ProviderError("Ada: ReadError from http://127.0.0.1:11434/v1"))
    assert is_retryable(ProviderError("Ada: ConnectError('connection reset')"))
    assert is_retryable(ProviderError("Ada: overloaded_error"))
    # River streams a 429 as an error frame inside a 200 response, with no status.
    assert is_retryable(
        ProviderError(
            "Lyra: {'message': 'Account request rate limit exceeded for this model', "
            "'type': 'rate_limit_error', 'code': 'account_rate'}"
        )
    )
    assert not is_retryable(ProviderError("Ada: no base URL or model"))
    # anything unrecognised is not retried: the default is conservative
    assert not is_retryable(ValueError("bad plan"))
    assert not is_retryable(BudgetExceeded("stop"))


def test_retry_after_is_a_floor_not_a_jittered_suggestion(tmp_path: Path) -> None:
    exc = ProviderError("Ada: HTTP 429 rate limit", status=429, retry_after=90.0)
    # jitter may add, never subtract, from what the provider asked for
    assert _delay(1, 0.75, exc) >= 90.0


def test_retry_recovers_from_a_transient_failure(tmp_path: Path) -> None:
    attempts: list[int] = []

    async def factory() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise ProviderError("Ada: HTTP 503 upstream", status=503)
        return "ok"

    assert asyncio.run(with_retries(factory, attempts=3, base_delay=0)) == "ok"
    assert len(attempts) == 3


def test_retry_gives_up_and_reraises(tmp_path: Path) -> None:
    async def factory() -> str:
        raise ProviderError("Ada: HTTP 503 upstream", status=503)

    try:
        asyncio.run(with_retries(factory, attempts=2, base_delay=0))
    except ProviderError:
        return
    raise AssertionError("a permanently failing call must surface its error")


# ---------- prompt provenance ----------


def test_prompt_fingerprints_track_wording(tmp_path: Path) -> None:
    assert prompts.fingerprint("write the file") != prompts.fingerprint("write the whole file")
    assert len(prompts.fingerprint("x")) == 8
    prints = prompts.fingerprints()
    for name in ("planner", "reason", "code", "review", "synthesis"):
        assert name in prints, f"{name} should be fingerprinted"


# ---------- verification ----------


def test_verify_flags_wiring_that_cannot_work(tmp_path: Path) -> None:
    files = {
        "index.html": {"content": '<html><body><button id="go">x</button><script src="app.js"></script></body></html>'},
        "styles.css": {"content": "body { color: red; }"},
        "app.js": {"content": "document.getElementById('missing').onclick = () => 1;"},
    }
    report = verify.check(files)
    codes = {issue["code"] for issue in report["issues"]}
    assert not report["ok"]
    assert "missing_id" in codes


def test_verify_flags_missing_assets_and_empty_files(tmp_path: Path) -> None:
    files = {
        "index.html": {"content": '<html><body><link rel="stylesheet" href="styles.css"></body></html>'},
        "app.js": {"content": "   "},
    }
    report = verify.check(files)
    codes = {issue["code"] for issue in report["issues"]}
    assert "missing_asset" in codes
    assert "empty" in codes
    assert report["errors"] >= 2


def test_verify_accepts_a_consistent_trio(tmp_path: Path) -> None:
    files = {
        "index.html": {
            "content": (
                '<html><body><div id="count">0</div>'
                '<link rel="stylesheet" href="styles.css">'
                '<script src="app.js"></script></body></html>'
            )
        },
        "styles.css": {"content": "#count { font-size: 2rem; }"},
        "app.js": {"content": "document.getElementById(\"count\").textContent = '1';"},
    }
    report = verify.check(files)
    assert report["ok"] and report["errors"] == 0, report["issues"]


def test_verify_checks_json_manifests(tmp_path: Path) -> None:
    files = {
        "package.json": {"content": '{"name": "x", "dependencies": {"a": "1",},}'},  # trailing comma
        "tsconfig.json": {"content": '{\n  // strict settings\n  "compilerOptions": { "strict": true }\n}'},
    }
    report = verify.check(files, list(files))
    broken = [item for item in report["issues"] if item["code"] == "invalid_json"]
    assert [item["file"] for item in broken] == ["package.json"], report["issues"]
    assert "does not parse" in broken[0]["detail"]


def test_verify_checks_relative_imports(tmp_path: Path) -> None:
    """A TSX app: extensionless, index and asset imports resolve; a ghost import does not."""
    files = {
        "src/main.tsx": {
            "content": (
                'import { App } from "./App";\nimport "./index.css";\nimport { Missing } from "./lib/Missing";\n'
            )
        },
        "src/App.tsx": {"content": 'import { Button } from "./ui";'},
        "src/ui/index.tsx": {"content": "export const Button = () => null;"},
        "src/index.css": {"content": "body { margin: 0; }"},
    }
    report = verify.check(files, list(files))
    unresolved = [item for item in report["issues"] if item["code"] == "unresolved_import"]
    assert len(unresolved) == 1, report["issues"]
    assert unresolved[0]["file"] == "src/main.tsx"
    assert "./lib/Missing" in unresolved[0]["detail"]


def test_verify_checks_python_relative_imports(tmp_path: Path) -> None:
    files = {
        "app.py": {
            "content": (
                "import os\nfrom fastapi import FastAPI\nfrom .core import helper\nfrom .missing import nothing\n"
            )
        },
        "core.py": {"content": "def helper():\n    return 1\n"},
    }
    report = verify.check(files, list(files))
    unresolved = [item for item in report["issues"] if item["code"] == "unresolved_import"]
    # stdlib and third-party imports are not guessed at, only the local one is checked
    assert len(unresolved) == 1, report["issues"]
    assert unresolved[0]["file"] == "app.py"
    assert "missing" in unresolved[0]["detail"]


def test_verify_checks_go_local_imports(tmp_path: Path) -> None:
    files = {
        "go.mod": {"content": "module example/app\n\ngo 1.22\n"},
        "main.go": {
            "content": (
                "package main\n\nimport (\n"
                '\t"fmt"\n'
                '\tapp "example/app/handlers"\n'
                '\t"example/app/missing"\n'
                ")\n\nfunc main() { fmt.Println(app.X) }\n"
            )
        },
        "handlers/handlers.go": {"content": "package handlers\n\nconst X = 1\n"},
    }
    report = verify.check(files, list(files))
    unresolved = [item for item in report["issues"] if item["code"] == "unresolved_import"]
    assert [item["file"] for item in unresolved] == ["main.go"], report["issues"]
    assert "example/app/missing" in unresolved[0]["detail"]


def test_verify_checks_rust_modules(tmp_path: Path) -> None:
    files = {
        "src/main.rs": {"content": 'mod parser;\nmod missing;\n\nfn main() { println!("hi"); }'},
        "src/parser.rs": {"content": "pub fn parse() {}"},
    }
    report = verify.check(files, list(files))
    unresolved = [item for item in report["issues"] if item["code"] == "unresolved_import"]
    assert [item["file"] for item in unresolved] == ["src/main.rs"], report["issues"]
    assert "missing" in unresolved[0]["detail"]


def test_verify_issues_become_reviewer_evidence(tmp_path: Path) -> None:
    report = verify.check({"index.html": {"content": ""}})
    evidence = verify.as_findings(report)
    assert "DETERMINISTIC CHECKS" in evidence
    assert "empty" in evidence
    assert verify.as_findings(verify.check({"a.txt": {"content": "hello"}})) == ""


# ---------- scaffolding follows the request ----------


def test_a_declared_file_without_a_unit_gets_a_file_specific_job(tmp_path: Path) -> None:
    raw = (
        '{"objective":"a portfolio","files":["src/main.tsx","src/App.tsx","src/styles.css",'
        '"src/components/Hero.tsx"],"work_units":[]}'
    )
    _, units, _, _ = planner_agent.parse(raw, "build a developer portfolio")
    briefs = {unit.file: unit.objective for unit in units}

    assert briefs["src/main.tsx"].startswith("Mount the root App")
    assert briefs["src/App.tsx"].startswith("Compose the page using only")
    assert "Hero" in briefs["src/App.tsx"]
    assert "classes" in briefs["src/styles.css"]
    assert "Hero" in briefs["src/components/Hero.tsx"]


def test_scaffold_follows_the_requested_stack(tmp_path: Path) -> None:
    _, react, react_files, _ = planner_agent.parse("not json at all", "build a react dashboard")
    # React scaffolds its own HTML shell and a real stylesheet, not inline-only styles.
    assert {"index.html", "src/App.tsx", "src/styles.css"} <= set(react_files)
    assert [u.file for u in react] == react_files

    _, python, python_files, _ = planner_agent.parse("", "a python fastapi service")
    assert "app.py" in python_files and "requirements.txt" in python_files
    assert all(path.endswith((".py", ".txt")) for path in python_files)

    _, vanilla, vanilla_files, _ = planner_agent.parse("", "a landing page for a bakery")
    assert vanilla_files == ["index.html", "styles.css", "app.js"]


def test_scaffold_ignores_substring_false_positives(tmp_path: Path) -> None:
    """'cli' inside 'client' and 'node' inside 'nodes' used to scaffold a TS CLI."""
    _, _, client_files, _ = planner_agent.parse("", "a client dashboard for our agency")
    assert client_files == ["index.html", "styles.css", "app.js"], client_files

    _, _, graph_files, _ = planner_agent.parse("", "visualise nodes and edges as a graph")
    assert graph_files == ["index.html", "styles.css", "app.js"], graph_files

    # a request that genuinely is a CLI still gets the node scaffold
    stack, units = planner_agent.infer_stack("a typescript cli that renames files")
    assert stack == "typescript (node)", stack
    assert "src/index.ts" in [unit.file for unit in units]


def test_scaffold_covers_vue_svelte_go_and_rust(tmp_path: Path) -> None:
    cases = {
        "build a vue admin panel": ("vue", "src/App.vue"),
        "a svelte storefront": ("svelte", "src/App.svelte"),
        "a go api server with sqlite": ("go", "main.go"),
        "a rust cli that tails logs": ("rust", "src/main.rs"),
    }
    for prompt, (stack, path) in cases.items():
        got_stack, units = planner_agent.infer_stack(prompt)
        assert got_stack == stack, (prompt, got_stack, stack)
        assert path in [unit.file for unit in units], (prompt, [unit.file for unit in units])

    _, go_units = planner_agent.infer_stack("a go api server with sqlite")
    assert [unit.file for unit in go_units] == ["go.mod", "main.go", "main_test.go"]


def test_scaffold_does_not_read_a_bare_go_as_golang(tmp_path: Path) -> None:
    """'go' is ordinary English, so it is matched only as 'golang' or a known phrase."""
    stack, units = planner_agent.infer_stack("a dashboard that updates as you go")
    assert stack == "html+css+js", stack
    assert [unit.file for unit in units] == ["index.html", "styles.css", "app.js"]


def test_scaffold_keeps_a_multi_language_plan(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "objective": "a small service",
            "stack": "python",
            "files": ["app.py", "core.py", "requirements.txt"],
            "work_units": [
                {"title": "Routes", "role": "backend", "file": "app.py", "objective": "the HTTP surface"},
                {"title": "Logic", "role": "backend", "file": "core.py", "objective": "pure functions"},
                {"title": "Deps", "role": "backend", "file": "requirements.txt", "objective": "pins"},
            ],
        }
    )
    _, units, files, _ = planner_agent.parse(raw, "a python service")
    assert files == ["app.py", "core.py", "requirements.txt"]
    assert [unit.file for unit in units] == files


def test_verify_attributes_a_missing_id_to_the_script(tmp_path: Path) -> None:
    files = {
        "index.html": {"content": '<html><body><script src="app.js"></script></body></html>'},
        "app.js": {"content": "document.getElementById('ghost');"},
        "styles.css": {"content": "body { margin: 0; }"},
    }
    report = verify.check(files)
    issue = next(item for item in report["issues"] if item["code"] == "missing_id")
    assert issue["file"] == "app.js", issue
    assert "index.html" in issue["detail"]


def test_every_issue_names_the_file_that_causes_it(tmp_path: Path) -> None:
    """Each code blames the file the edit belongs in, not a neighbour."""
    files = {
        "index.html": {
            "content": (
                '<html><body><link rel="stylesheet" href="missing.css">'
                '<div id="count">0</div><script src="app.js"></script></body></html>'
            )
        },
        "styles.css": {"content": "body { color: red;"},  # unclosed brace
        "app.js": {"content": "document.getElementById('ghost');\n// TODO: finish"},
        "empty.js": {"content": ""},
    }
    report = verify.check(files, list(files))
    by_code: dict[str, list[dict]] = {}
    for item in report["issues"]:
        by_code.setdefault(item["code"], []).append(item)

    # the link lives in the markup, so the markup owns the missing asset
    assert by_code["missing_asset"][0]["file"] == "index.html", by_code["missing_asset"]
    # the call lives in the script, so the script owns the missing id
    missing = by_code["missing_id"][0]
    assert missing["file"] == "app.js", missing
    assert "index.html" in missing["detail"], missing
    assert by_code["unbalanced_braces"][0]["file"] == "styles.css", by_code["unbalanced_braces"]
    assert by_code["placeholder"][0]["file"] == "app.js", by_code["placeholder"]
    assert by_code["empty"][0]["file"] == "empty.js", by_code["empty"]
    # a script's mistake is never filed against the page
    assert not any(item["code"] == "missing_id" and item["file"] == "index.html" for item in report["issues"])


def test_verify_reads_ids_from_typescript_scripts(tmp_path: Path) -> None:
    """Scaffolding emits .tsx now, so a TSX ghost id must be caught and attributed."""
    files = {
        "index.html": {"content": '<html><body><div id="root"></div><script src="app.js"></script></body></html>'},
        "app.js": {"content": "document.getElementById('root');"},
        "src/App.tsx": {"content": "const el = document.getElementById('ghost');"},
    }
    report = verify.check(files, list(files))
    missing = [item for item in report["issues"] if item["code"] == "missing_id"]
    assert len(missing) == 1, report["issues"]
    assert missing[0]["file"] == "src/App.tsx", missing


# ---------- safety paths ----------


def test_budget_stops_a_run_before_the_first_provider_call(tmp_path: Path) -> None:
    original_root = state_mod.RUNS_ROOT
    state_mod.RUNS_ROOT = tmp_path
    try:
        planner = Specialist(
            id="p1", name="Ada", role="planner", model="gpt-4o-mini", baseUrl="https://api.openai.com/v1"
        )
        run = RunState(run_id="budget-stop", prompt="build a page", specialists=[planner])
        run.budget.spend(run.budget.max_tokens, 0.0)
        asyncio.run(orch.Orchestrator(run).run_all())

        kinds = [event["type"] for event in run.events]
        assert kinds[-2:] == ["budget", "error"], kinds
        assert run.done is False
        budget_event = next(event for event in run.events if event["type"] == "budget")
        assert budget_event["tokens"] >= run.budget.max_tokens
        # the ledger is persisted even when the run is cut short
        ledger_path = tmp_path / "budget-stop" / ".harness-metrics.json"
        assert ledger_path.exists()
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        assert ledger["done"] is False
        assert ledger["budget"]["tokens"] >= run.budget.max_tokens
        assert ledger["budget"]["remaining_tokens"] == 0
    finally:
        state_mod.RUNS_ROOT = original_root


def test_budget_stops_a_run_after_work_has_started(tmp_path: Path) -> None:
    """Planning and scaffolding succeed, then the cap refuses the next request.

    The fake planner spends through `runtime.current_budget()`, so this also proves the
    orchestrator's binding reaches nested node calls rather than only its own frame.
    """
    original_root = state_mod.RUNS_ROOT
    original_plan = planner_agent.plan
    state_mod.RUNS_ROOT = tmp_path
    planned: list[str] = []

    async def fake_plan(agent, prompt, memory=""):  # noqa: ANN001 - `memory` is the planner's new argument
        budget = runtime.current_budget()
        assert budget is not None, "the orchestrator must bind the run budget for nested calls"
        budget.spend(budget.max_tokens, 0.0)
        units = [WorkUnit(id="wu-1", title="Markup", role="frontend", file="index.html", objective="markup")]
        planned.append(prompt)
        return "a page", units, ["index.html"], [], llm.Usage()

    planner_agent.plan = fake_plan
    try:
        planner = Specialist(
            id="p1", name="Ada", role="planner", model="gpt-4o-mini", baseUrl="https://api.openai.com/v1"
        )
        run = RunState(run_id="budget-mid", prompt="build a page", specialists=[planner])
        run.budget.max_tokens = 120
        asyncio.run(orch.Orchestrator(run).run_all())

        kinds = [event["type"] for event in run.events]
        assert "plan" in kinds and "scaffold" in kinds, kinds
        assert kinds[-2:] == ["budget", "error"], kinds
        assert "done" not in kinds
        assert run.done is False
        assert planned == ["build a page"]
        # the stop happened after real work: the scaffolded file is on disk
        assert (tmp_path / "budget-mid" / "index.html").exists()

        ledger = json.loads((tmp_path / "budget-mid" / ".harness-metrics.json").read_text(encoding="utf-8"))
        assert ledger["done"] is False
        assert ledger["files"] == 1
        assert ledger["budget"]["tokens"] >= 120
    finally:
        planner_agent.plan = original_plan
        state_mod.RUNS_ROOT = original_root


class _StubRun:
    """Minimal run: a budget and an event sink, which is all the client needs."""

    def __init__(self, budget: Budget):
        self.budget = budget
        self.run_id = "stub"
        self.events: list[dict] = []

    async def emit(self, event: dict) -> None:
        self.events.append(event)


def test_cache_hit_returns_the_answer_without_spending(tmp_path: Path) -> None:
    original_enabled, original_temp = llm.CACHE_ENABLED, llm.TEMPERATURE
    messages = [{"role": "user", "content": "the same request"}]
    stub = _StubRun(Budget(max_tokens=1000, max_usd=1.0))
    CACHE.clear()
    llm.CACHE_ENABLED = True
    llm.TEMPERATURE = 0.0  # the only setting the cache is allowed to replay
    runtime.bind(stub)
    try:
        CACHE.set(spec(), messages, 0.0, {"text": "cached answer"})
        text, usage = asyncio.run(llm.call(spec(), messages))
    finally:
        llm.CACHE_ENABLED, llm.TEMPERATURE = original_enabled, original_temp
        CACHE.clear()
        runtime.RUN.set(None)

    assert text == "cached answer"
    assert usage.cached == 1 and usage.calls == 0 and usage.cost_usd == 0.0
    assert stub.budget.tokens == 0 and stub.budget.calls == 0
    # the saving is reported to the run rather than hidden
    assert any(event.get("kind") == "cache" for event in stub.events), stub.events


def test_verification_overrules_an_approved_review(tmp_path: Path) -> None:
    original_root = state_mod.RUNS_ROOT
    original_review = review_node.reviewer.review
    original_revise = review_node.revise
    revised: list[str] = []
    evidence_seen: list[str] = []
    revision_inputs: list[str] = []

    async def approved(*_args, **kwargs):
        evidence_seen.append(kwargs.get("evidence", ""))
        return "APPROVED", llm.Usage()

    async def fake_revise(_run, _reviewer, unit, findings, _think):
        revised.append(unit.file)
        revision_inputs.append(findings)

    state_mod.RUNS_ROOT = tmp_path
    review_node.reviewer.review = approved
    review_node.revise = fake_revise
    try:
        author = Specialist(
            id="a1", name="Ada", role="frontend", model="gpt-4o-mini", baseUrl="https://api.openai.com/v1"
        )
        reviewer = Specialist(
            id="r1", name="Rex", role="reviewer", model="gpt-4o-mini", baseUrl="https://api.openai.com/v1"
        )
        run = RunState(run_id="overrule", prompt="a counter", specialists=[author, reviewer])
        run.files = {
            "index.html": {
                "agent": "a1",
                "content": '<html><body><span id="count">0</span><script src="app.js"></script></body></html>',
                "done": True,
            },
            "app.js": {"agent": "a1", "content": "document.getElementById('ghost').textContent = 1;", "done": True},
        }
        units = [WorkUnit(id="wu-1", title="Wiring", role="frontend", file="app.js", objective="wire it", agent="a1")]

        findings = asyncio.run(review_node.run(run, reviewer, units))

        assert revised == ["app.js"], revised
        assert "DETERMINISTIC CHECKS" in findings, findings
        assert "overrules" in findings, findings
        assert run.verification is not None and run.verification["ok"] is False, run.verification
        # the reviewer was shown the facts, and the revision loop received them
        assert evidence_seen and "DETERMINISTIC CHECKS" in evidence_seen[0], evidence_seen
        assert revision_inputs and "missing_id" in revision_inputs[0], revision_inputs
        assert "revising" in [event.get("phase") for event in run.events if event.get("type") == "phase"]
        messages = [event for event in run.events if event.get("type") == "message"]
        assert any(event.get("from") == "verify" and "disagree" in event.get("text", "") for event in messages), (
            messages
        )
    finally:
        review_node.reviewer.review = original_review
        review_node.revise = original_revise
        state_mod.RUNS_ROOT = original_root


# ---------- multi-file answers ----------


def test_marker_stream_splits_each_file_onto_its_own_path(tmp_path: Path) -> None:
    router = executor.CodeStream("src/App.tsx")
    chunks = router.feed(
        "thinking about the tree\n"
        "<<<FILE src/App.tsx>>>\n"
        "```tsx\n"
        'export const App = () => <p id="hi">hi</p>;\n'
        "```\n"
        "<<<FILE ./src/index.css>>>\n"
        "#hi { color: red; }\n"
    )
    assert [kind for kind, _, _ in chunks].count("marker") == 2
    code = [(path, text) for kind, path, text in chunks if kind == "code"]
    assert [path for path, _ in code] == ["src/App.tsx", "src/index.css"]

    rendered = dict(router.rendered())
    assert set(rendered) == {"src/App.tsx", "src/index.css"}
    assert "export const App" in rendered["src/App.tsx"]
    assert "color" not in rendered["src/App.tsx"]
    assert "color: red" in rendered["src/index.css"]


def test_marker_files_are_registered_written_and_verified(tmp_path: Path) -> None:
    original_root = state_mod.RUNS_ROOT
    original_stream = llm.stream
    state_mod.RUNS_ROOT = tmp_path

    async def fake_stream(agent, messages, think=True, stats=None):  # noqa: ANN001
        if "Think before writing" in messages[0]["content"]:
            for line in ("1. RESTATE the tree.\n", "2. DECISION one component.\n"):
                stats.content.append(line)
                yield ("think", line)
            return
        for line in (
            "<<<FILE src/App.tsx>>>\n",
            "```tsx\n",
            'export const App = () => <p id="hi">hi</p>;\n',
            "```\n",
            "<<<FILE src/index.css>>>\n",
            "#hi { color: red; }\n",
        ):
            stats.content.append(line)
            yield ("content", line)

    llm.stream = fake_stream
    try:
        agent = Specialist(id="a1", name="Ada", role="frontend", model="stub")
        run = RunState(run_id="markers", prompt="a page", specialists=[agent])
        unit = WorkUnit(id="wu-1", title="UI", role="frontend", file="src/App.tsx", objective="the app", agent="a1")
        asyncio.run(execution.write_unit(run, unit, []))

        assert sorted(run.files) == ["src/App.tsx", "src/index.css"]
        assert all(meta["done"] for meta in run.files.values())
        workspace = tmp_path / "markers"
        assert "color: red" in (workspace / "src/index.css").read_text(encoding="utf-8")
        assert "color: red" not in (workspace / "src/App.tsx").read_text(encoding="utf-8")
        ends = {event["path"] for event in run.events if event["type"] == "file_end"}
        assert ends == {"src/App.tsx", "src/index.css"}
        assert unit.status == "done" and unit.lines >= 1
        assert {item["path"] for item in run.file_summary()} == {"src/App.tsx", "src/index.css"}
        assert verify.check(run.files, sorted(run.files))["ok"] is True
    finally:
        llm.stream = original_stream
        state_mod.RUNS_ROOT = original_root


def test_a_unit_that_writes_no_file_is_asked_again(tmp_path: Path) -> None:
    """Every planned file gets a second ask: an empty answer is a failed unit, not a file.

    The clean-state gate treats a planned file nobody wrote as an error, so a unit that
    silently produced nothing used to leave the run unrecoverable. The retry names the
    empty answer in the second request.
    """
    original_root = state_mod.RUNS_ROOT
    original_stream = llm.stream
    state_mod.RUNS_ROOT = tmp_path
    asks: list[str] = []

    async def fake_stream(agent, messages, think=True, stats=None):  # noqa: ANN001
        if "Think before writing" in messages[0]["content"]:
            stats.content.append("1. RESTATE the file.\n")
            yield ("think", "1. RESTATE the file.\n")
            return
        ask = messages[-1]["content"]
        # The plan critic runs at every depth now; only the author's code asks matter to
        # this test. (The `quick` comment in the old version predates that change.)
        if not ask.startswith(("Write app.js", "Your previous answer")):
            return
        asks.append(ask)
        if len(asks) == 1:
            return  # the model answered with nothing at all
        for line in ("<<<FILE app.js>>>\n", 'document.body.dataset.ready = "1";\n'):
            stats.content.append(line)
            yield ("content", line)

    llm.stream = fake_stream
    try:
        agent = Specialist(id="a1", name="Ada", role="frontend", model="stub")
        # `quick` skips the critic pass, so the two compose calls are the only code asks.
        run = RunState(run_id="retried", prompt="a page", specialists=[agent], depth="quick")
        unit = WorkUnit(id="wu-1", title="Logic", role="frontend", file="app.js", objective="the page", agent="a1")
        asyncio.run(execution.write_unit(run, unit, []))

        # Every depth now runs the plan critic, so the two code asks are not the only
        # calls this stream sees. Keep the ones the retry loop sends to the author.
        code_asks = [text for text in asks if text.startswith(("Write app.js", "Your previous answer"))]
        assert len(code_asks) == 2, "an empty first answer must be retried, not accepted"
        assert "previous answer" in code_asks[1]
        assert run.files["app.js"]["done"] is True
        assert "dataset.ready" in (tmp_path / "retried" / "app.js").read_text(encoding="utf-8")
        assert unit.status == "done"
    finally:
        llm.stream = original_stream
        state_mod.RUNS_ROOT = original_root


def test_marker_cannot_escape_the_run_workspace(tmp_path: Path) -> None:
    original_root = state_mod.RUNS_ROOT
    original_stream = llm.stream
    state_mod.RUNS_ROOT = tmp_path

    async def fake_stream(agent, messages, think=True, stats=None):  # noqa: ANN001
        if "Think before writing" in messages[0]["content"]:
            yield ("think", "plan\n")
            return
        for line in ("<<<FILE ../escape.txt>>>\n", "pwned\n"):
            stats.content.append(line)
            yield ("content", line)

    llm.stream = fake_stream
    try:
        agent = Specialist(id="a1", name="Ada", model="stub")
        run = RunState(run_id="escape-run", prompt="x", specialists=[agent])
        unit = WorkUnit(id="wu-1", title="Evil", role="general", file="app.js", objective="x", agent="a1")
        asyncio.run(execution.write_unit(run, unit, []))
    finally:
        llm.stream = original_stream
        state_mod.RUNS_ROOT = original_root

    assert not (tmp_path / "escape.txt").exists()
    assert "escape.txt" not in run.files
    assert any(event["type"] == "error" and "outside the run workspace" in event["message"] for event in run.events)


# ---------- the runner ----------


def test_runner_serves_a_static_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "static-run"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html><body>served for real</body></html>", encoding="utf-8")
    # the human checkpoint is satisfied explicitly; the pipeline is what is under test
    service = runner.Service(run_id="static-run", workspace=workspace, approved=True)

    async def check() -> None:
        await service.start()
        try:
            assert service.state == "ready" and service.kind == "static", service.as_dict()
            assert service.root == workspace
            assert service.url and service.url.startswith("http://127.0.0.1:")
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(service.url)
            assert response.status_code == 200
            assert "served for real" in response.text
        finally:
            await service.stop()

    asyncio.run(check())
    assert service.state == "stopped"


def test_runner_restart_rebuilds_instead_of_reusing_the_running_service(tmp_path: Path) -> None:
    workspace = tmp_path / "restart-run"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html><body>v1</body></html>", encoding="utf-8")

    async def wait_ready(service: runner.Service, timeout: float = 20.0) -> runner.Service:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if service.state in ("ready", "failed", "stopped"):
                return service
            await asyncio.sleep(0.1)
        raise AssertionError(f"service stuck in {service.state}")

    async def check() -> None:
        first = await runner.start("restart-run", workspace, approved=True)
        try:
            await wait_ready(first)
            assert first.state == "ready", first.as_dict()

            # Edit the tree, then restart: the old server must die and the pipeline rerun.
            (workspace / "index.html").write_text("<html><body>v2</body></html>", encoding="utf-8")
            second = await runner.start("restart-run", workspace, restart=True, approved=True)
            assert second is not first, "restart handed back the already-running service"
            await wait_ready(second)
            assert second.state == "ready", second.as_dict()
            assert first.state == "stopped" and first.exit_code is not None, first.as_dict()
            assert any("static_server.py" in line for line in second.logs), second.logs
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(second.url)
            assert response.status_code == 200 and "v2" in response.text
        finally:
            await runner.stop("restart-run")
            runner.SERVICES.pop("restart-run", None)

    asyncio.run(check())


def test_cancelling_a_run_kills_its_install_step(tmp_path: Path) -> None:
    """A restart during `npm install` must not leave the old installer running."""
    workspace = tmp_path / "cancel-run"
    workspace.mkdir()
    pidfile = workspace / "installer.pid"
    service = runner.Service(run_id="cancel-run", workspace=workspace, approved=True)
    installer = [
        sys.executable,
        "-c",
        "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)",
        str(pidfile),
    ]

    async def check() -> None:
        task = asyncio.create_task(service._exec(installer, workspace, 120))
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the install step never started")

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        pid = int(pidfile.read_text())
        for _ in range(60):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"installer {pid} survived the cancellation")

    asyncio.run(check())


def test_static_server_falls_back_to_index_for_client_routes(tmp_path: Path) -> None:
    workspace = tmp_path / "spa-run"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html><body>app shell</body></html>", encoding="utf-8")
    (workspace / "app.js").write_text("console.log('app')\n", encoding="utf-8")
    service = runner.Service(run_id="spa-run", workspace=workspace, approved=True)

    async def check() -> None:
        await service.start()
        try:
            assert service.state == "ready" and service.url
            async with httpx.AsyncClient(timeout=5.0) as client:
                route = await client.get(f"{service.url}settings/profile")
                slash = await client.get(f"{service.url}about/")
                nested = await client.get(f"{service.url}docs/guides/")
                query = await client.get(f"{service.url}about/?ref=mail")
                asset = await client.get(f"{service.url}app.js")
                missing = await client.get(f"{service.url}missing.js")
            assert route.status_code == 200 and "app shell" in route.text
            assert slash.status_code == 200 and "app shell" in slash.text
            assert nested.status_code == 200 and "app shell" in nested.text
            assert query.status_code == 200 and "app shell" in query.text
            assert asset.status_code == 200 and "console.log" in asset.text
            assert missing.status_code == 404, "a missing asset must still be a 404"
        finally:
            await service.stop()

    asyncio.run(check())


def test_runner_reports_a_tree_it_cannot_run(tmp_path: Path) -> None:
    workspace = tmp_path / "empty-run"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("nothing to run", encoding="utf-8")

    service = runner.Service(run_id="empty-run", workspace=workspace)
    asyncio.run(service.start())

    assert service.state == "failed"
    assert "nothing here can run" in service.detail
    assert service.logs and service.logs[-1].startswith("!!")


def _api(tmp_path: Path, token: str = "test-token"):
    """A TestClient with RUNS_ROOT and the roster pointed at this test's tree."""
    import os

    from fastapi.testclient import TestClient

    from ai_harness import main as api
    from ai_harness.modules.roster import RosterStore

    # `asyncio.run` in an earlier test clears the default loop on Python 3.9, and the
    # TestClient needs one to exist. Cheap to guarantee here rather than order-dependent.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    previous = os.environ.get("HARNESS_TOKEN")
    os.environ["HARNESS_TOKEN"] = token
    runs = tmp_path / "runs"
    runs.mkdir()
    from ai_harness import paths

    # Both apps resolve a run id through `paths`, which reads its own module binding.
    original_runs, original_roster = paths.RUNS_ROOT, api.ROSTER
    paths.RUNS_ROOT = runs
    api.ROSTER = RosterStore(tmp_path / ".harness-roster.json")

    def restore() -> None:
        paths.RUNS_ROOT, api.ROSTER = original_runs, original_roster
        if previous is None:
            os.environ.pop("HARNESS_TOKEN", None)
        else:
            os.environ["HARNESS_TOKEN"] = previous

    return TestClient(api.app, base_url="http://127.0.0.1:5178"), runs, restore


ALLOWED = {"Origin": "http://127.0.0.1:5178"}


def test_run_reads_cannot_escape_the_runs_root(tmp_path: Path) -> None:
    client, runs, restore = _api(tmp_path)
    try:
        (runs / "abc").mkdir()
        (runs / "abc" / "index.html").write_text("hi", encoding="utf-8")
        # shares a string prefix with the workspace: the case `startswith` let through
        (runs / "abc-sibling").mkdir()
        (runs / "abc-sibling" / "secret.txt").write_text("SECRET", encoding="utf-8")

        assert (
            client.get("/api/run/abc/file", params={"path": "../abc-sibling/secret.txt"}, headers=ALLOWED).status_code
            == 404
        )
        assert client.get("/api/run/abc/file", params={"path": "/etc/passwd"}, headers=ALLOWED).status_code == 404
        assert client.get("/api/run/%2E%2E/file", params={"path": "secret.txt"}, headers=ALLOWED).status_code == 404
        # the route is gone from the API origin entirely; no files are served from it
        assert client.get("/preview/abc/%2e%2e/abc-sibling/secret.txt", headers=ALLOWED).status_code == 404

        served = client.get("/api/run/abc/file", params={"path": "index.html"}, headers=ALLOWED)
        assert served.status_code == 200 and served.json()["content"] == "hi"
    finally:
        restore()


def test_api_refuses_a_foreign_origin_and_requires_the_token(tmp_path: Path) -> None:
    client, _runs, restore = _api(tmp_path, token="test-token")
    try:
        assert client.get("/api/runs", headers={"Origin": "http://evil.example"}).status_code == 403
        assert client.get("/api/runs", headers=ALLOWED).status_code == 200
        # a script that presents neither an origin nor the token is refused
        assert client.get("/api/runs").status_code == 401
        assert client.get("/api/runs", headers={"X-Harness-Token": "wrong"}).status_code == 401
        assert client.get("/api/runs", headers={"X-Harness-Token": "test-token"}).status_code == 200
        # liveness stays readable so a UI can report the harness is down
        assert client.get("/api/health").status_code == 200
    finally:
        restore()


def test_dns_rebinding_cannot_use_the_same_origin_shortcut(tmp_path: Path) -> None:
    from ai_harness import main as api

    # The rule itself: Origin equal to Host is same-origin only when that host is loopback.
    for origin, host, expected in [
        ("http://evil.example:8000", "evil.example:8000", False),  # rebound name, netlocs agree
        ("http://evil.example:8000", "127.0.0.1:8000", False),  # rebound name, real loopback
        ("http://127.0.0.1:8000", "127.0.0.1:8000", True),
        ("http://localhost:8000", "localhost:8000", True),
        ("http://[::1]:8000", "[::1]:8000", True),
        ("http://127.0.0.1:5178", "127.0.0.1:8000", True),  # the dev UI's allowlisted origin
        ("http://127.0.0.1:8000.evil.example", "127.0.0.1:8000.evil.example", False),
        ("", "127.0.0.1:8000", False),
    ]:
        assert api._origin_allowed(origin, host) is expected, (origin, host)

    client, _runs, restore = _api(tmp_path)
    try:
        # A rebound name resolves to 127.0.0.1, so Origin and Host agree — and are not loopback.
        rebound = {"Origin": "http://evil.example:8000", "Host": "evil.example:8000"}
        assert client.get("/api/runs", headers=rebound).status_code == 403
        assert (
            client.get("/api/runs", headers={"Sec-Fetch-Site": "same-origin", "Host": "evil.example:8000"}).status_code
            == 403
        )

        local = {"Host": "127.0.0.1:8000"}
        assert client.get("/api/runs", headers={**local, "Sec-Fetch-Site": "same-origin"}).status_code == 401
        assert client.get("/api/runs", headers={**local, "Origin": "http://127.0.0.1:8000"}).status_code == 200
        # the dev UI is still trusted through the explicit origin list
        assert client.get("/api/runs", headers=ALLOWED).status_code == 200
    finally:
        restore()


def test_artifacts_live_on_their_own_origin(tmp_path: Path) -> None:
    """A run's files are not served by the API's origin, and that origin has no API."""
    from fastapi.testclient import TestClient

    from ai_harness import preview

    client, runs, restore = _api(tmp_path)
    try:
        (runs / "abc").mkdir()
        (runs / "abc" / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")

        # the API origin no longer serves generated files at all
        assert client.get("/preview/abc/index.html").status_code == 404

        artifacts = TestClient(preview.app)
        # ... and the artifact origin has no API for its own scripts to drive
        assert artifacts.get("/api/roster").status_code == 404
        assert artifacts.get("/api/run").status_code == 404

        served = artifacts.get("/preview/abc/index.html")
        assert served.status_code == 200 and "hi" in served.text
        assert served.headers["cache-control"] == "no-store"

        # containment still holds on the new origin
        assert artifacts.get("/preview/abc/..%2f..%2fetc/passwd").status_code in (403, 404)
        assert artifacts.get("/preview/..%2fabc/index.html").status_code in (403, 404)
        assert artifacts.get("/preview/nope/index.html").status_code == 404
    finally:
        restore()


def test_live_service_is_reachable_only_with_its_token(tmp_path: Path) -> None:
    """A served app is arbitrary code, so the proxy demands its per-run secret."""
    from ai_harness import preview

    workspace = tmp_path / "live-run"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html><body>proxied app</body></html>", encoding="utf-8")
    service = runner.Service(run_id="live-run", workspace=workspace, approved=True)
    runner.SERVICES["live-run"] = service  # the proxy resolves it through the registry

    async def check() -> None:
        await service.start()
        try:
            assert service.state == "ready" and service.port, service.as_dict()
            transport = httpx.ASGITransport(app=preview.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://artifacts.test") as client:
                assert (await client.get("/live/live-run/wrong-token/")).status_code == 403
                assert (await client.get("/live/ghost/whatever/")).status_code == 404
                ok = await client.get(f"/live/live-run/{service.share}/")
                assert ok.status_code == 200 and "proxied app" in ok.text
                assert ok.headers.get("cache-control") == "no-store"
        finally:
            await service.stop()

    try:
        asyncio.run(check())
    finally:
        runner.SERVICES.pop("live-run", None)


def test_a_served_tree_is_only_reachable_under_its_run_nonce(tmp_path: Path) -> None:
    """Header checks cannot apply to a navigation, so the path carries the run's nonce."""
    workspace = tmp_path / "nonce-run"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html><body>behind the nonce</body></html>", encoding="utf-8")
    service = runner.Service(run_id="nonce-run", workspace=workspace, approved=True)

    async def check() -> None:
        await service.start()
        try:
            assert service.state == "ready" and service.port and service.url, service.as_dict()
            base = f"http://127.0.0.1:{service.port}"
            async with httpx.AsyncClient(timeout=5.0) as client:
                served = await client.get(service.url)
                assert served.status_code == 200 and "behind the nonce" in served.text
                assert service.share in service.url, "the URL has to carry the nonce"

                # a page that merely scans loopback ports gets nothing
                assert (await client.get(f"{base}/")).status_code == 404
                assert (await client.get(f"{base}/index.html")).status_code == 404
                assert (await client.get(f"{base}/some-other-nonce/")).status_code == 404

                # inside the nonce the tree behaves as before: routes, assets, redirect
                route = await client.get(f"{base}/{service.share}/settings")
                assert route.status_code == 200 and "behind the nonce" in route.text
                assert (await client.get(f"{base}/{service.share}")).status_code == 302
        finally:
            await service.stop()

    asyncio.run(check())


def test_a_partial_roster_push_cannot_delete_stored_specialists(tmp_path: Path) -> None:
    """Starting a run publishes what a browser knows; it must not erase the rest."""
    from ai_harness import main as api

    client, _runs, restore = _api(tmp_path)
    try:
        first = {
            "id": "kept",
            "name": "Kept",
            "role": "frontend",
            "kind": "openai",
            "baseUrl": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "apiKey": "sk-keep-me",
        }
        client.put("/api/roster", json={"specialists": [first]}, headers=ALLOWED)

        # a browser that only knows about one specialist starts a run
        scout = {**first, "id": "scout", "name": "Scout", "apiKey": ""}
        client.put("/api/roster", json={"specialists": [scout]}, headers=ALLOWED)

        ids = [spec["id"] for spec in client.get("/api/roster", headers=ALLOWED).json()["specialists"]]
        assert ids == ["kept", "scout"], ids
        assert api.ROSTER.get("kept").apiKey == "sk-keep-me"

        # an explicit workshop save is still allowed to remove one
        client.put("/api/roster", json={"specialists": [scout], "replace": True}, headers=ALLOWED)
        ids = [spec["id"] for spec in client.get("/api/roster", headers=ALLOWED).json()["specialists"]]
        assert ids == ["scout"], ids
    finally:
        restore()


def test_a_stored_key_is_cleared_only_when_asked(tmp_path: Path) -> None:
    """A blank key is "unchanged", so deleting one has to be an explicit request."""
    from ai_harness import main as api

    client, _runs, restore = _api(tmp_path)
    try:
        entry = {
            "id": "scout",
            "name": "Scout",
            "role": "frontend",
            "kind": "openai",
            "baseUrl": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "apiKey": "sk-live",
        }
        client.put("/api/roster", json={"specialists": [entry], "replace": True}, headers=ALLOWED)
        assert client.get("/api/roster", headers=ALLOWED).json()["specialists"][0]["has_key"] is True

        # a masked edit that sends no key leaves the stored one alone
        masked = {**entry, "apiKey": ""}
        client.put("/api/roster", json={"specialists": [masked], "replace": True}, headers=ALLOWED)
        assert client.get("/api/roster", headers=ALLOWED).json()["specialists"][0]["has_key"] is True

        # asking to clear it drops the key, and the specialist is no longer runnable
        clearing = {**masked, "clearKey": True}
        stored = client.put("/api/roster", json={"specialists": [clearing], "replace": True}, headers=ALLOWED)
        assert stored.status_code == 200, stored.text
        spec = client.get("/api/roster", headers=ALLOWED).json()["specialists"][0]
        assert spec["has_key"] is False, spec
        assert spec["configured"] is False, spec
        assert api.ROSTER.get("scout").apiKey == "", "the key must not survive on disk"

        # a key typed in the same save wins over the clear flag
        typed = {**entry, "apiKey": "sk-new", "clearKey": True}
        client.put("/api/roster", json={"specialists": [typed], "replace": True}, headers=ALLOWED)
        assert api.ROSTER.get("scout").apiKey == "sk-new"
    finally:
        restore()


def test_roster_keeps_keys_on_the_server(tmp_path: Path) -> None:
    from ai_harness import main as api

    client, _runs, restore = _api(tmp_path)
    try:
        entry = {
            "id": "vega",
            "name": "Vega",
            "role": "frontend",
            "kind": "openai",
            "baseUrl": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "apiKey": "sk-secret",
        }
        assert client.put("/api/roster", json={"specialists": [entry]}, headers=ALLOWED).status_code == 200

        view = client.get("/api/roster", headers=ALLOWED)
        body = view.json()["specialists"][0]
        assert "apiKey" not in body and body["has_key"] is True and body["configured"] is True
        assert "sk-secret" not in view.text

        # a run never receives a key from the browser; the server supplies the stored one
        spec = api._specialist(api.SpecialistIn(**{k: v for k, v in entry.items() if k != "apiKey"}))
        assert spec.apiKey == "sk-secret"

        # a blank key means "unchanged", so a masked edit cannot erase it
        client.put("/api/roster", json={"specialists": [{**entry, "apiKey": "", "name": "Vega II"}]}, headers=ALLOWED)
        assert api.ROSTER.get("vega").apiKey == "sk-secret"
        assert (api.ROSTER.path.stat().st_mode & 0o777) == 0o600
    finally:
        restore()


if __name__ == "__main__":
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    fn(Path(tmp).resolve())
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    print(f"FAIL {name}: {exc}")
                else:
                    print(f"ok   {name}")
    raise SystemExit(1 if failures else 0)
