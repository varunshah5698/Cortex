"""Build the animated overview page from a real run.

Reads one run's event log and files, then writes a self-contained
`public/overview.html` (no network, no dependencies) that plays the build
environment back as a looping animation.

    ./.venv/bin/python scripts/build_overview.py [run_id]
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "workspace" / "runs"
TEMPLATE = ROOT / "scripts" / "overview.html"
OUT = ROOT / "public" / "overview.html"
CODE_LINES = 64


def clip(text: str, limit: int) -> str:
    """Cut a quote at a sentence end so the film never shows half a sentence."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = cut.rfind(". ")
    return (cut[: stop + 1] if stop > limit // 3 else cut.rsplit(" ", 1)[0] + "…").strip()


def latest_build_run() -> str:
    best, best_n = None, -1
    for path in RUNS.iterdir():
        log = path / "events.jsonl"
        if not log.exists():
            continue
        events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        kinds = {e.get("type") for e in events}
        if not {"start", "plan", "file_end", "done"} <= kinds:
            continue
        if len(events) > best_n:
            best, best_n = path.name, len(events)
    if not best:
        raise SystemExit("no completed build run found in workspace/runs")
    return best


def collect(run_id: str) -> dict:
    run = RUNS / run_id
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines() if line.strip()]
    by = lambda kind: [e for e in events if e.get("type") == kind]  # noqa: E731

    start = by("start")[0]
    plan = (by("plan") or [{"units": []}])[0]
    scaffold = (by("scaffold") or [{"files": []}])[0]
    done = (by("done") or [{}])[0]
    metrics = done.get("metrics") or {}

    verify = []
    for event in by("verify"):
        verify.append(
            {
                "stage": event.get("stage", "check"),
                "errors": event.get("errors", 0),
                "warnings": event.get("warnings", 0),
                "checked": event.get("checked", 0),
                "issues": [
                    {"file": i.get("file", ""), "code": i.get("code", ""), "detail": i.get("detail", "")}
                    for i in event.get("issues", [])
                ][:3],
            }
        )
    # Which files the reviewer sent back between passes: rewritten after the first check.
    revised: list[str] = []
    for event in by("file_end"):
        if event.get("revised") and event["path"] not in revised:
            revised.append(event["path"])
    if verify and revised:
        verify[0]["revised"] = revised

    sources, queries = [], []
    for event in by("research"):
        query = event.get("query", "")
        if query and query not in queries:
            queries.append(query)
        url = event.get("url", "")
        host = urlparse(url).netloc.replace("www.", "") if url else "local"
        entry = {"title": event.get("title") or url, "host": host, "url": url}
        if entry not in sources:
            sources.append(entry)

    files = done.get("files") or [
        {"path": e["path"], "agent": e["agent"], "lines": e.get("lines", 0), "revised": bool(e.get("revised"))}
        for e in by("file_end")
    ]
    order: list[str] = []
    for event in by("file_start"):
        if event["path"] not in order:
            order.append(event["path"])
    files.sort(key=lambda f: order.index(f["path"]) if f["path"] in order else 99)

    code = {}
    for f in files:
        path = run / f["path"]
        if path.exists():
            lines = path.read_text(errors="replace").splitlines()[:CODE_LINES]
            code[f["path"]] = [line if len(line) <= 58 else line[:55] + "..." for line in lines]

    totals = metrics.get("totals", {})
    budget = metrics.get("budget", {})
    created = start.get("created") or ""
    try:
        when = dt.datetime.fromisoformat(created)
        date_label = when.strftime("%d %b %Y · %H:%M UTC")
    except ValueError:
        date_label = created[:16]

    # The two briefs the film shows are the ones the room itself wrote: the planner's
    # scaffold note and the first thing said to everyone during the research phase.
    plan_note, research_note = "", ""
    phase = ""
    for event in events:
        if event.get("type") == "phase":
            phase = event.get("phase", "")
        elif event.get("type") == "message":
            if not plan_note and event.get("kind") == "plan":
                plan_note = event.get("text", "")
            elif not research_note and phase == "research" and event.get("to") == "all":
                research_note = event.get("text", "")

    return {
        "runId": run_id,
        "planNote": clip(plan_note, 150),
        "researchNote": clip(research_note, 300),
        "dateLabel": date_label,
        "depth": metrics.get("depth", "deep"),
        "model": (metrics.get("agents") or [{}])[0].get("model", "local model"),
        "prompt": start.get("prompt", ""),
        "plan": [
            {
                "id": u.get("id", ""),
                "title": u.get("title", ""),
                "role": u.get("role", "general"),
                "file": u.get("file", ""),
                "agent": u.get("agent", ""),
                "brief": (u.get("objective", "").split(" Request:")[0] or "")[:96],
            }
            for u in plan.get("units", [])
        ],
        "scaffold": scaffold.get("files", []),
        "queries": queries,
        "sources": sources[:6],
        "verify": verify,
        "files": [
            {
                "path": f["path"],
                "agent": f.get("agent") or "",
                "lines": f.get("lines", 0),
                "revised": bool(f.get("revised")),
            }
            for f in files
        ],
        "code": code,
        "agents": [
            {
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "role": a.get("role", "general"),
                "model": a.get("model", ""),
                "total_tokens": a.get("total_tokens", 0),
                "ms": a.get("ms", 0),
                "calls": a.get("calls", 0),
            }
            for a in metrics.get("agents", [])
        ],
        "totals": {
            "total_tokens": totals.get("total_tokens", 0),
            "calls": totals.get("calls", 0),
            "ms": totals.get("ms", 0),
            "thinking_chars": totals.get("thinking_chars", 0),
            "cost_usd": totals.get("cost_usd", 0.0),
        },
        "budget": {
            "max_tokens": budget.get("max_tokens", 0),
            "max_usd": budget.get("max_usd", 0.0),
            "remaining_tokens": budget.get("remaining_tokens", 0) or 0,
        },
        "cache_hits": (metrics.get("cache") or {}).get("run_hits", 0),
    }


def main() -> None:
    run_id = sys.argv[1] if len(sys.argv) > 1 else latest_build_run()
    data = collect(run_id)
    # Every `<` is escaped: file bodies contain markup ("<!--", "<script") and an inline
    # script must not let the HTML parser wander into escaped-script states.
    payload = json.dumps(data).replace("<", "\\u003c")
    html = TEMPLATE.read_text().replace("__DATA_JSON__", payload)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html)
    print(
        f"wrote {OUT.relative_to(ROOT)} from run {run_id} ({len(data['files'])} files, {len(data['sources'])} sources)"
    )


if __name__ == "__main__":
    main()
