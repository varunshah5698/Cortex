"""The run receipt: what a person needs to review a run in thirty seconds.

"Done" in this harness means a green terminal event, and that is a verdict, not an
account. A receipt is the account: the request, what shipped and who wrote it, which
checks ran and what they concluded, what it cost, what a human approved, and what is
still open. It is written next to the run as `RECEIPT.md` and served read-only from
`/api/run/{id}/receipt`, so a run can be reviewed without replaying its transcript.

Everything in it comes from facts already on disk — the event log and the metrics ledger —
so a receipt can be rebuilt for a finished run after a restart, and never adds a model
call to produce.
"""

from __future__ import annotations

import json
from pathlib import Path

FILENAME = "RECEIPT.md"


def _events(run) -> list[dict]:
    return list(getattr(run, "events", []) or [])


def build(run) -> dict:
    """The receipt as data: every field traceable to an event or the ledger."""
    events = _events(run)
    checks = [event for event in events if event.get("type") == "verify"]
    tools = [event for event in events if event.get("type") == "tool"]
    approvals = [event for event in events if event.get("type") == "approval"]
    verification = run.verification or {}
    open_issues = [
        f"{item.get('file')} · {item.get('code')}: {item.get('detail')}"
        for item in verification.get("issues") or []
        if item.get("level") == "error"
    ]
    return {
        "run_id": run.run_id,
        "prompt": run.prompt,
        "created": run.created,
        "done": bool(run.done),
        "depth": run.depth,
        "files": run.file_summary(),
        "checks": [
            {
                "stage": event.get("stage"),
                "ok": bool(event.get("ok")),
                "errors": event.get("errors", 0),
                "warnings": event.get("warnings", 0),
                "checked": event.get("checked", 0),
            }
            for event in checks
        ],
        "verification": {
            "ok": bool(verification.get("ok")),
            "stage": verification.get("stage"),
            "errors": verification.get("errors", 0),
        },
        "open_issues": open_issues[:12],
        "cost": run.metrics().get("totals", {}),
        "budget": run.budget.as_dict(),
        "tools": {
            "calls": len(tools),
            "failed": sum(1 for event in tools if not event.get("ok", True)),
            "names": sorted({str(event.get("tool")) for event in tools}),
        },
        "approvals": [str(event.get("command") or event.get("commands") or "") for event in approvals],
        "errors": [str(event.get("message"))[:200] for event in events if event.get("type") == "error"][:5],
    }


def render(receipt: dict) -> str:
    """Markdown, because the reader is a person and the file should stand on its own."""
    status = "shipped" if receipt["done"] else "stopped short"
    lines = [
        f"# Run receipt · `{receipt['run_id']}`",
        "",
        f"**{status}** · {receipt['created']} · depth `{receipt['depth']}`",
        "",
        "## Request",
        "",
        f"{receipt['prompt'][:900]}",
        "",
        f"## Files ({len(receipt['files'])})",
        "",
    ]
    if receipt["files"]:
        lines += ["| file | owner | lines | state |", "| --- | --- | --- | --- |"]
        for item in receipt["files"]:
            state = "revised" if item.get("revised") else ("written" if item.get("done") else "open")
            lines.append(f"| `{item['path']}` | {item.get('agent') or '—'} | {item.get('lines', 0)} | {state} |")
    else:
        lines.append("_No files were written._")

    lines += ["", "## Checks", ""]
    if receipt["checks"]:
        lines += ["| stage | verdict | errors | warnings |", "| --- | --- | --- | --- |"]
        for check in receipt["checks"]:
            lines.append(
                f"| {check['stage']} | {'ok' if check['ok'] else 'fail'} | {check['errors']} | {check['warnings']} |"
            )
    else:
        lines.append("_No deterministic check ran._")

    if receipt["open_issues"]:
        lines += ["", "## Open issues", ""]
        lines += [f"- {item}" for item in receipt["open_issues"]]

    if receipt["errors"]:
        lines += ["", "## Errors", ""]
        lines += [f"- {item}" for item in receipt["errors"]]

    cost = receipt["cost"]
    lines += [
        "",
        "## Cost",
        "",
        f"{cost.get('total_tokens', 0):,} tokens · {cost.get('calls', 0)} calls · "
        f"{cost.get('ms', 0) / 1000:.1f}s of model time · ${cost.get('cost_usd', 0):.4f}",
        "",
        f"Budget: {receipt['budget'].get('tokens', 0):,} tokens, "
        f"${receipt['budget'].get('usd', 0):.4f}, {receipt['budget'].get('calls', 0)} calls.",
        "",
        "## Tools",
        "",
        f"{receipt['tools']['calls']} call(s), {receipt['tools']['failed']} failed — "
        + (", ".join(receipt["tools"]["names"]) or "none"),
    ]
    if receipt["approvals"]:
        lines += ["", "## Approved by the operator", ""]
        lines += [f"- `{item}`" for item in receipt["approvals"] if item]
    return "\n".join(lines) + "\n"


def write(run) -> Path | None:
    """Write `RECEIPT.md` into the run's workspace. Never raises: a receipt is a courtesy."""
    receipt = build(run)
    path = Path(run.workspace) / FILENAME
    try:
        path.write_text(render(receipt), encoding="utf-8")
        (Path(run.workspace) / ".harness-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return path
    except OSError:
        return None


def read(workspace: Path) -> str | None:
    path = workspace / FILENAME
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
