"""`python -m ai_harness.evals` — run the golden cases and report a score.

Exit status is 1 when a case misses its expected verification verdict, when `--build`
found a tree that does not compile, or when a score drops below the saved baseline, so
this can gate a change without a human reading the table.

A `--judge` score is never part of that: it is printed and saved, and the deterministic
checks stay the only gate (see `evals/judge.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .runner import load_baseline, run_suite, save, write_baseline


def _judge_cell(judged: dict | None) -> str:
    """The judge column: 0.70, `?` for an unreadable answer, `-` when not run."""
    if not judged:
        return "-"
    value = judged.get("value")
    return "?" if value is None else f"{value:.2f}"


def _print_table(results: list[dict]) -> None:
    width = max(16, *(len(item["case"]) for item in results))
    print(f"{'case':<{width}}  score  verify  build  judge  ms")
    for item in results:
        verdict = "ok" if item["verification_ok"] else "fail"
        tier = item.get("build") or {}
        served = "-" if not tier or tier.get("skipped") else ("ok" if tier["ok"] else "fail")
        judged = item.get("judge")
        print(
            f"{item['case']:<{width}}  {item['passed']}/{item['total']}    {verdict:<5}  "
            f"{served:<5}  {_judge_cell(judged):<5}  {item['ms']}"
        )
        for message in item["errors"]:
            print(f"{'':<{width}}  ! {message}")
        if served == "fail":
            problem = tier.get("problem") or f"{tier.get('state')} · {tier.get('detail')}"
            print(f"{'':<{width}}  ! {problem}")
        if judged:
            note = judged.get("error") or judged.get("reason")
            print(f"{'':<{width}}  ~ judged by {judged.get('judged_by')}: {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_harness.evals",
        description="Score the harness on fixed cases. Scripted by default; --live spends credits.",
    )
    parser.add_argument("--live", action="store_true", help="use the configured roster instead of the script")
    parser.add_argument("--case", action="append", default=[], help="run only this case (repeatable)")
    parser.add_argument("--json", action="store_true", help="print the raw result JSON")
    parser.add_argument("--compare", action="store_true", help="fail if a case scores below the baseline")
    parser.add_argument(
        "--update-baseline", action="store_true", help="record this run as the baseline to compare against"
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="also serve each servable case's tree through modules/runner.py (installs and builds)",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="also grade each live run with a rubric call (advisory: never changes the exit status)",
    )
    args = parser.parse_args(argv)
    if args.judge and not args.live:
        # The judge sends the request to a real model: it is part of the live tier, and it
        # must not quietly spend credits from a command that looked free.
        parser.error("--judge needs --live: it asks a configured model to grade the run, and spends credits")

    results = asyncio.run(run_suite(live=args.live, only=args.case, build=args.build, judge=args.judge))
    path = save(results)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_table(results)

    failed = [
        item["case"]
        for item in results
        if not item["checks"]["verification"]
        or not item["checks"]["reported_done"]
        or not item["checks"].get("flagged", True)
    ]
    for name in failed:
        print(f"failed: {name} did not reach its expected verdict", file=sys.stderr)
    # A tree that never reached a ready service is a failure of the same weight: the
    # facts about its files were the only thing that looked right.
    for item in results:
        tier = item.get("build") or {}
        if tier and not tier.get("skipped") and not tier.get("ok"):
            failed.append(item["case"])
            print(
                f"failed: {item['case']} did not reach a ready service ({tier.get('state')} · {tier.get('detail')})",
                file=sys.stderr,
            )
    print(f"results: {path}", file=sys.stderr)

    if args.update_baseline:
        print(f"baseline: {write_baseline(results)}", file=sys.stderr)

    if args.compare:
        baseline = load_baseline()
        if not baseline:
            print("baseline: none saved yet; run with --update-baseline", file=sys.stderr)
        else:
            for item in results:
                before = baseline.get(item["case"])
                if before is not None and item["passed"] < before:
                    print(f"regressed: {item['case']} {before} -> {item['passed']}", file=sys.stderr)
                    failed.append(item["case"])

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
