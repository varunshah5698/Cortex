"""Review node: the sensor of the harness — findings, questions, rewrites."""

from __future__ import annotations

import asyncio
import posixpath
import re

from ..agents import executor, reviewer
from ..config.settings import AGENT_LOOP, AGENT_STEPS
from ..core.state import RunState
from ..models import Specialist, WorkUnit
from ..modules import agent_loop, context, goal, intent, llm, memory, progress, prompts, thrash
from ..nodes import execution, verify


def _browser_findings(run: RunState) -> str:
    report = getattr(run, "browser", None) or {}
    issues = [item for item in report.get("issues") or [] if isinstance(item, dict)]
    if not issues:
        return ""
    lines = ["BROWSER CHECK (a real Chrome drove the served page):"]
    for item in issues[:12]:
        lines.append(
            f"- [{item.get('level', 'error')}] {item.get('file', 'page')} · {item.get('code', 'browser')}: {item.get('detail', '')}"
        )
    lines.append("These are observed in the running app. Fix the page before calling it done.")
    return "\n".join(lines)


async def run(run: RunState, agent: Specialist, units: list[WorkUnit]) -> str:
    await run.emit({"type": "phase", "phase": "reviewing"})
    await run.emit({"type": "status", "agent": agent.id, "status": "reviewing"})

    think = lambda text: run.emit({"type": "think", "agent": agent.id, "delta": text})  # noqa: E731

    # A deterministic sensor first: facts the reviewer must not be able to miss.
    checks = await verify.run(run, "post-build")
    memory.learn(checks)
    memory.learn(getattr(run, "browser", None))
    browser = _browser_findings(run)
    try:
        findings, usage = await reviewer.review(
            agent,
            run.prompt,
            run.files,
            think,
            evidence="\n\n".join(text for text in (verify.as_findings(checks), browser) if text),
        )
    except llm.ProviderError as exc:
        await run.emit({"type": "error", "message": str(exc), "agent": agent.id})
        # The reviewer never ran, so nothing here is approved. Recording the pre-review
        # checks unchanged let a provider outage reach `done` on a tree no model had
        # reviewed; force the verdict to a failure so `conclude` refuses and emits a
        # blocked `error` instead of a green run.
        run.verification = {
            **checks,
            "ok": False,
            "stage": "review-unavailable",
            "errors": max(1, int(checks.get("errors") or 0)),
        }
        return ""
    await run.emit(run.track_usage(agent, "review", usage))
    await run.emit({"type": "status", "agent": agent.id, "status": "done"})
    await run.emit({"type": "message", "from": agent.id, "text": findings[:900], "kind": "review"})

    approved = bool(re.fullmatch(r"\s*APPROVED\s*", findings, flags=re.I))
    if approved and checks["ok"] and not browser:
        run.verification = checks
        await run.emit({"type": "message", "from": agent.id, "text": "Approved — nothing to fix.", "kind": "review"})
        return findings
    if approved and (not checks["ok"] or browser):
        # The model approved but a machine-measured fact disagrees (files or the live
        # browser). Trust the measurement; the later block appends it to the findings.
        await run.emit(
            {
                "type": "message",
                "from": "verify",
                "text": "Reviewer approved, but the harness checks disagree — fixing the facts first.",
                "kind": "review",
            }
        )
        findings = (findings.rstrip() + "\n\nReviewer said APPROVED; the harness overrules that.").strip()

    # The reviewer writes prose; the verifier writes facts. A rewrite must obey both, so
    # the deterministic issues are quoted verbatim and their files are always targeted —
    # otherwise a vague review "fix" leaves the real defect in place.
    if checks["issues"] or browser:
        findings = "\n\n".join(
            text for text in (findings.rstrip(), verify.as_findings(checks), browser) if text
        ).strip()
    failing = {item["file"] for item in checks.get("issues", [])}
    targets = [u for u in units if u.file in failing] + [u for u in units if u.file.lower() in findings.lower()]
    flagged = list({u.id: u for u in targets}.values())[:2] or units[:1]
    await run.emit({"type": "phase", "phase": "revising"})
    await _revise_all(run, agent, flagged, findings, think)
    run.verification = await verify.run(run, "post-revision")
    return findings


# ---------- deterministic wiring ----------
#
# Two models writing two halves of one page disagree on names: the markup declares
# `btnStartPause`, the script queries `startBtn`. Prompting harder does not fix that —
# a weak model repeats the same invention on every rewrite. So the harness closes the
# gap itself before spending another model round: script selectors are mapped onto ids
# the markup actually declares (token match, one id per selector), ids nobody declares
# get a real element, and the markup finally loads the script and stylesheet it wrote.

CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|\d+")
QUOTED = "['\"]"
WRAPPER_TOKENS = {"btn", "button", "id", "el", "element"}


def _tokens(value: str) -> list[str]:
    return [part.lower() for part in CAMEL.findall(value)]


def _label(wanted: str) -> str:
    words = [word for word in _tokens(wanted) if word not in WRAPPER_TOKENS]
    return " ".join(word.capitalize() for word in words) or "Button"


def _element(wanted: str) -> str:
    """The smallest element that can honestly carry this id."""
    tokens = set(_tokens(wanted))
    if tokens & {"btn", "button", "toggle", "submit"}:
        return f'<button type="button" id="{wanted}">{_label(wanted)}</button>'
    if tokens & {"list", "history", "log", "items"}:
        return f'<ul id="{wanted}"></ul>'
    if tokens & {"status", "message", "note", "error", "label"}:
        return f'<p id="{wanted}"></p>'
    return f'<div id="{wanted}"></div>'


def _match(wanted: str, declared: set[str], taken: set[str]) -> str | None:
    """The declared id whose tokens already contain every token the script asked for."""
    want = set(_tokens(wanted))
    if not want:
        return None
    best: tuple[int, str] | None = None
    for candidate in declared - taken:
        have = set(_tokens(candidate))
        if not want <= have:
            continue
        score = (len(have - want), candidate)
        if best is None or score < best:
            best = score
    return best[1] if best else None


def _rel(markup: str, target: str) -> str:
    return posixpath.relpath(target, posixpath.dirname(markup) or ".")


def _splice(html: str, closing: str, snippet: str) -> str:
    index = html.lower().rfind(closing.lower())
    if index == -1:
        index = html.lower().rfind("</body>")
    if index == -1:
        return html.rstrip() + "\n" + snippet + "\n"
    return html[:index] + snippet + "\n" + html[index:]


async def _rewrite(run: RunState, path: str, content: str, why: str) -> None:
    meta = run.files.setdefault(path, {"agent": None, "content": "", "done": True})
    meta.update({"content": content, "done": True, "repaired": True})
    run.fm.write(path, content)
    await run.emit({"type": "message", "from": "verify", "text": why, "kind": "work"})
    await run.emit(
        {
            "type": "file_end",
            "path": path,
            "agent": meta.get("agent") or "verify",
            "lines": content.count("\n") + 1,
            "repaired": True,
        }
    )


async def wire(run: RunState) -> list[str]:
    """Make the markup and the scripts agree, deterministically. Returns what changed."""
    markup_path = next(
        (
            path
            for path, meta in run.files.items()
            if path.endswith((".html", ".htm")) and (meta.get("content") or "").strip()
        ),
        None,
    )
    if not markup_path:
        return []
    html = run.files[markup_path]["content"]
    declared = set(verify.ID_IN_HTML.findall(html))
    scripts = {
        path: meta.get("content") or ""
        for path, meta in run.files.items()
        if path.endswith(verify.SCRIPT_SUFFIXES) and (meta.get("content") or "").strip()
    }

    changes: list[str] = []
    missing: list[str] = []
    driver = ""
    for path, script in scripts.items():
        queried = list(dict.fromkeys(verify.GET_BY_ID.findall(script) + verify.QUERY_ID.findall(script)))
        # Only a script that shares vocabulary with this markup is this markup's script.
        # A file that queries no id the page declares (and no id that token-matches one)
        # is a different defect — a script for another page, or a hallucinated interface
        # — and the verifier keeps reporting it instead of the harness inventing a DOM.
        related = bool(set(queried) & declared) or any(_match(item, declared, set()) for item in queried)
        if not related:
            continue
        wanted = [item for item in queried if item not in declared]
        if not wanted:
            continue
        wanted.sort(key=script.find)
        renamed: dict[str, str] = {}
        taken: set[str] = set()
        remaining = list(wanted)
        # Exact token-set matches first (resetBtn ↔ btnReset), then the smallest declared
        # id that already contains every token the script asked for.
        for wanted_id in list(remaining):
            exact = [c for c in declared - taken if set(_tokens(c)) == set(_tokens(wanted_id))]
            if len(exact) == 1:
                renamed[wanted_id] = exact[0]
                taken.add(exact[0])
                remaining.remove(wanted_id)
        for wanted_id in list(remaining):
            found = _match(wanted_id, declared, taken)
            if found:
                renamed[wanted_id] = found
                taken.add(found)
                remaining.remove(wanted_id)

        if renamed:
            updated = script
            for old, new in renamed.items():
                updated = re.sub(
                    rf"({QUOTED})(#?){re.escape(old)}\1",
                    lambda match, new=new: f"{match.group(1)}{match.group(2)}{new}{match.group(1)}",
                    updated,
                )
            if updated != script:
                await _rewrite(
                    run,
                    path,
                    updated,
                    f"Wiring: {path} now uses " + ", ".join(f"#{old} → #{new}" for old, new in renamed.items()),
                )
                changes.append(f"{path}: " + ", ".join(f"#{old}→#{new}" for old, new in renamed.items()))
        if renamed:
            driver = driver or path
        missing.extend(item for item in remaining if item not in declared)

    html_new = html
    if missing:
        snippet = "\n".join("    " + _element(item) for item in sorted(set(missing)))
        html_new = _splice(html_new, "</body>", snippet)
        changes.append(f"{markup_path}: declared " + ", ".join(f"#{item}" for item in sorted(set(missing))))
    if driver and not verify.SCRIPT_TAG.search(html_new):
        html_new = _splice(html_new, "</body>", f'    <script src="{_rel(markup_path, driver)}" defer></script>')
        changes.append(f"{markup_path}: loads {driver}")
    if not verify.STYLESHEET_LINK.search(html_new):
        sheets = [
            path for path, meta in run.files.items() if path.endswith(".css") and (meta.get("content") or "").strip()
        ]
        if sheets:
            sheet = max(sheets, key=lambda path: len(run.files[path]["content"]))
            html_new = _splice(html_new, "</head>", f'    <link rel="stylesheet" href="{_rel(markup_path, sheet)}">')
            changes.append(f"{markup_path}: loads {sheet}")

    if html_new != html:
        await _rewrite(run, markup_path, html_new, "Wiring: " + "; ".join(changes))
    return changes


async def _revise_all(
    run: RunState,
    agent: Specialist,
    units: list[WorkUnit],
    findings: str,
    think,
    use_loop: bool = True,
) -> None:
    """Rewrite in parallel, except on a local runner where calls queue anyway."""

    async def one(unit: WorkUnit) -> None:
        # The 5-argument call is the old signature tests monkeypatch; only the fallback
        # path (which must not re-enter the loop it just failed) passes the extra flag.
        if use_loop:
            await revise(run, agent, unit, findings, think)
        else:
            await revise(run, agent, unit, findings, think, use_loop=False)

    if all(spec.is_local for spec in run.specialists):
        for unit in units:
            await one(unit)
        return
    # One unit raising must not cancel its siblings mid-write: an un-awaited cancel leaves
    # a half-written file behind and the run reports a failure it cannot explain. Collect
    # the exceptions and name the file that failed instead.
    results = await asyncio.gather(*(one(unit) for unit in units), return_exceptions=True)
    for unit, result in zip(units, results):  # noqa: B905 - the project venv is Python 3.9
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            await run.emit(
                {
                    "type": "error",
                    "agent": agent.id,
                    "message": f"revising {unit.file} failed: {type(result).__name__}: {result}",
                }
            )


async def _agent_repair(run: RunState, targets: list[WorkUnit], findings: str) -> set[str]:
    """Let each author repair its own file with tools. Returns the paths worth keeping.

    A path is returned only when the loop actually changed the file. A model that cannot
    produce a usable tool call returns nothing, so the caller still has the whole-file
    rewrite as a fallback — the loop is an upgrade, not a new way to get stuck.
    """

    async def one(unit: WorkUnit) -> str | None:
        author = next((s for s in run.specialists if s.id == unit.agent), run.specialists[0])
        await run.emit({"type": "status", "agent": author.id, "status": "thinking"})
        try:
            changed = await agent_loop.repair(
                run,
                author,
                unit.file,
                findings,
                lambda: verify.run(run, "agent-check"),
                max_steps=AGENT_STEPS,
            )
        except llm.ProviderError as exc:
            await run.emit({"type": "error", "agent": author.id, "message": str(exc)})
            changed = False
        except thrash.Thrash as exc:
            await run.emit({"type": "error", "agent": author.id, "message": str(exc)})
            changed = False
        await run.emit({"type": "status", "agent": author.id, "status": "done" if changed else "idle"})
        return unit.file if changed else None

    # One writer at a time: every loop checks the same tree, so running two in parallel
    # would let one agent's partial edit appear in the other's check. The steps are
    # bounded and each call is small; correctness is worth the serialization.
    results = [await one(unit) for unit in targets]
    return {path for path in results if path}


async def repair(run: RunState, agent: Specialist, units: list[WorkUnit], rounds: int = 1) -> dict:
    """Fix what the deterministic checks still find, then re-check.

    The orchestrator calls this after review: a run whose files contradict themselves
    does not get to finish, so the facts get one bounded round of their own. Files the
    review never saw a unit for (written behind a `<<<FILE path>>>` marker) get a
    synthetic unit so their author is still the one who fixes them.
    """
    think = lambda text: run.emit({"type": "think", "agent": agent.id, "delta": text})  # noqa: E731

    # Names, not prose: wire first, for free. This is the part a model keeps getting
    # wrong, and it is mechanical — the markup is the interface, so the script adapts.
    report = run.verification or {"ok": True, "issues": []}
    if await wire(run):
        report = await verify.run(run, "post-wiring")
        run.verification = report
    for _ in range(max(0, rounds)):
        # Fail closed: an absent `ok` is not a pass. See the same rule in
        # `orchestrator.conclude` and `agent_loop.LoopState.finish`.
        if report.get("ok") is True:
            return report
        failing = {str(item.get("file") or "") for item in report.get("issues", []) if item.get("level") == "error"}
        targets = [unit for unit in units if unit.file in failing][:3]
        covered = {unit.file for unit in targets}
        for path in sorted(failing - covered):
            if not path or path not in run.files:
                continue
            author = str((run.files.get(path) or {}).get("agent") or "")
            targets.append(
                WorkUnit(id=f"repair:{path}", title=f"Fix {path}", role="repair", file=path, objective="", agent=author)
            )
        if not targets:
            return report
        findings = verify.as_findings(report)
        await run.emit({"type": "phase", "phase": "revising"})
        await run.emit(
            {
                "type": "message",
                "from": "verify",
                "text": f"Repairing {len(targets)} file(s) before this run can be called done.",
                "kind": "review",
            }
        )
        handled = await _agent_repair(run, targets, findings) if AGENT_LOOP else set()
        remaining = [unit for unit in targets if unit.file not in handled]
        if remaining:
            # A model that could not use the loop still gets the old path. Nothing is
            # left un-repaired merely because the tool protocol was too much for it —
            # and this path does not re-enter the loop it just failed.
            await _revise_all(run, agent, remaining, findings, think, use_loop=False)
        report = await verify.run(run, "post-repair")
    run.verification = report
    return report


async def revise(
    run: RunState,
    reviewer_agent: Specialist,
    unit: WorkUnit,
    findings: str,
    think,
    use_loop: bool = True,
) -> None:
    author = next((s for s in run.specialists if s.id == unit.agent), run.specialists[0])
    meta = run.files.get(unit.file)
    if not meta:
        return
    current = str(meta.get("content", "")).strip()

    # A review finding is usually one broken name. Let the author read the actual file
    # and patch that name, instead of paying for a Q&A and a full regeneration. A model
    # that cannot speak the tool protocol returns False and the old rewrite still runs.
    if AGENT_LOOP and use_loop:
        await run.emit({"type": "status", "agent": author.id, "status": "thinking"})
        try:
            changed = await agent_loop.repair(
                run,
                author,
                unit.file,
                findings,
                lambda: verify.run(run, "agent-check"),
                max_steps=AGENT_STEPS,
            )
        except llm.ProviderError as exc:
            await run.emit({"type": "error", "message": str(exc), "agent": author.id})
            changed = False
        except thrash.Thrash as exc:
            await run.emit({"type": "error", "message": str(exc), "agent": author.id})
            changed = False
        if changed:
            unit.status = "revised"
            progress.tick(run, unit.file)
            await run.emit({"type": "status", "agent": author.id, "status": "done"})
            await run.emit(
                {
                    "type": "message",
                    "from": author.id,
                    "text": f"Repaired {unit.file} with the tool loop after review.",
                    "kind": "revise",
                }
            )
            return

    try:
        await run.emit({"type": "status", "agent": author.id, "status": "talking"})
        asked, ask_usage = await reviewer.question(reviewer_agent, author.name, unit.file, findings[:2400])
        await run.emit(run.track_usage(reviewer_agent, "review-q", ask_usage))
        await run.emit(
            {"type": "message", "from": reviewer_agent.id, "to": author.id, "text": asked[:300], "kind": "talk"}
        )

        answered, answer_usage = await reviewer.reply(author, reviewer_agent.name, unit.file, findings[:2400], asked)
        await run.emit(run.track_usage(author, "reply", answer_usage))
        await run.emit(
            {"type": "message", "from": author.id, "to": reviewer_agent.id, "text": answered[:300], "kind": "talk"}
        )

        await run.emit({"type": "status", "agent": author.id, "status": "thinking"})
        deliberation, plan_usage = await reviewer.plan_fix(
            author,
            unit.file,
            findings[:2400],
            answered,
            on_think=lambda t: run.emit({"type": "think", "agent": author.id, "delta": t}),
        )
        await run.emit(run.track_usage(author, "fix-plan", plan_usage))

        await run.emit({"type": "status", "agent": author.id, "status": "writing"})
        sink = execution.FileSink(run, author, mode="rewrite")

        files, usage = await executor.compose(
            author,
            [
                {
                    "role": "system",
                    "content": prompts.CODE.format(
                        name=author.name,
                        role=author.role,
                        file=unit.file,
                        objective=unit.objective,
                        prompt=run.prompt,
                        acceptance=intent.contract(getattr(run, "acceptance", None) or []),
                        goal=prompts.GOAL_CONTRACT.format(goal=goal.read(run)),
                        reasoning=deliberation or answered,
                        critique=findings[:2400],
                        design=prompts.DESIGN_CONTRACT,
                        contracts=run.contracts.get(author.id, ""),
                        peer_files=context.files_context(run.files, exclude=unit.file),
                        lessons=memory.lessons(),
                        progress=progress.orient(run),
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Current {unit.file} — keep every id, class, selector and event handler that already works:\n"
                        f"```\n{current[:24000]}\n```\n\n"
                        f"Rewrite {unit.file} with the fixes. Reply with the whole file."
                        if current
                        else f"Rewrite {unit.file} with the fixes. Reply with the whole file."
                    ),
                },
            ],
            on_think=lambda t: run.emit({"type": "think", "agent": author.id, "delta": t}),
            on_code=sink.write,
            on_marker=sink.start,
            default_path=unit.file,
        )
        await run.emit(run.track_usage(author, "rewrite", usage))

        shipped = await sink.finish(files)
        if not shipped:
            await run.emit(
                {"type": "error", "agent": author.id, "message": f"{author.name} wrote nothing for {unit.file}"}
            )
            await run.emit({"type": "status", "agent": author.id, "status": "error"})
            return

        unit.status = "revised"
        progress.tick(run, unit.file)
        await run.emit({"type": "status", "agent": author.id, "status": "done"})
        await run.emit(
            {
                "type": "message",
                "from": author.id,
                "text": f"Revised {', '.join(shipped)} after review",
                "kind": "revise",
            }
        )
    except llm.ProviderError as exc:
        await run.emit({"type": "error", "message": str(exc), "agent": author.id})
