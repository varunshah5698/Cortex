"""Acceptance layer: the request becomes a checklist the harness can check.

`nodes/verify.py` proves the tree is internally consistent. `modules/design.py` proves it
was designed. Neither answers the only question the person who asked actually has: *does
it do what I asked for?* A weak model will happily ship a beautiful, well-tokenised page
that contains none of the features in the request, and every existing sensor passes it.

This module closes that gap without asking a model for an opinion. It reads the request,
extracts the capabilities it names as acceptance criteria, persists them beside the run,
injects them into every code prompt, and then checks the written tree for a trace of each.

Why deterministic extraction rather than a planner call: a 1B local model cannot be
trusted to enumerate its own requirements, and the whole point of the layer is that a
weak model benefits from it. A lexicon of capability vocabulary is cheap, offline,
reproducible and — most importantly — auditable: every criterion names the words that
satisfy it, so a finding can always be argued with.

Severity is chosen so the sensor cannot cry wolf:

* **error** — the build ignores the request: two or more capabilities were named and the
  tree contains no trace of any of them, or an interactive request produced a page with
  no script and no control at all.
* **warn** — one named capability has no trace, or fewer than half do. The repair loop
  gets the exact gap, which is where the harness already turns facts into fixes.

A warning never fails a run on its own. The two error cases are the ones where the
artefact is unambiguously not the thing that was asked for.
"""

from __future__ import annotations

import re
from pathlib import Path

FILENAME = "acceptance.md"
MAX_CRITERIA = 24
# How many individual gaps to name before summarising. A wall of 20 warnings is noise;
# the aggregate counts carry the rest.
MAX_GAP_FINDINGS = 8

SCRIPT_SUFFIXES = (".js", ".mjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte")
CONTROL_ELEMENT = re.compile(r"<(?:button|input|select|textarea|form)\b", re.I)
SCRIPT_ELEMENT = re.compile(r"<script\b", re.I)

# (kind, trigger, label, evidence, hard)
#
# `trigger` reads the request; `evidence` reads the tree. Evidence is deliberately the
# capability's own vocabulary: a model that implements search names it "search", so the
# check is a trace test rather than a guess about intent. A capability that is genuinely
# implemented always leaves its word behind; one that was forgotten leaves nothing.
RULES: list[tuple[str, str, str, str, bool]] = [
    # --- controls: the request names an action the person must be able to take ------
    ("control", r"\bsearch(?:able|es|ing|ed)?\b", "a search control", r"\bsearch", True),
    ("control", r"\bfilter(?:s|ing|ed|able)?\b", "a filter control", r"\bfilter", True),
    ("control", r"\bsort(?:s|ing|ed|able)?\b", "a sort control", r"\bsort", True),
    ("control", r"\badd(?:s|ing|ed)?\b|\bcreate\b", "an add/create action", r"\badd|\bcreate", True),
    ("control", r"\bdelet(?:e|es|ing|ed)\b|\bremove\b", "a delete action", r"\bdelete|\bremove|\btrash", True),
    ("control", r"\bedit(?:s|ing|ed)?\b|\bupdate\b", "an edit action", r"\bedit|\bupdate|\bmodify", True),
    ("control", r"\btoggl(?:e|es|ing|ed)\b", "a toggle", r"\btoggle", True),
    (
        "control",
        r"\bsav(?:e|es|ing|ed)\b|\bpersist",
        "a save action",
        r"\bsave|\bpersist|localStorage|sessionStorage",
        True,
    ),
    ("control", r"\breset\b|\bclear\b", "a reset/clear action", r"\breset|\bclear", False),
    ("control", r"\bupload(?:s|ing|ed)?\b", "an upload control", r"\bupload|type=[\"']file", True),
    (
        "control",
        r"\bdownload(?:s|ing|ed)?\b|\bexport\b",
        "a download/export action",
        r"\bdownload|\bexport|\bblob",
        True,
    ),
    ("control", r"\bsubmit(?:s|ting|ted)?\b", "a submit action", r"\bsubmit", True),
    (
        "control",
        r"\bcalculat(?:e|es|ing|ed|or)\b|\bconvert\b",
        "a calculate/convert action",
        r"\bcalculat|\bconvert|\bresult",
        True,
    ),
    ("control", r"\bplay(?:s|ing|ed)?\b|\bpaus(?:e|es|ing|ed)\b", "play/pause controls", r"\bplay|\bpause", False),
    ("control", r"\bdrag(?:s|ging|ged)?\b|\bdrop\b", "drag-and-drop", r"\bdrag|\bdrop", False),
    (
        "control",
        r"\bbookmark(?:s|ing|ed)?\b|\bfavou?rite\b|\blike\b",
        "a bookmark/favourite action",
        r"\bbookmark|\bfavou?rite|\blike",
        False,
    ),
    ("control", r"\bshar(?:e|es|ing|ed)\b", "a share action", r"\bshare", False),
    ("control", r"\bcopy\b|\bclipboard\b", "a copy action", r"\bcopy|clipboard", False),
    ("control", r"\bundo\b|\bredo\b", "undo/redo", r"\bundo|\bredo", False),
    ("control", r"\bzoom(?:s|ing|ed)?\b", "a zoom control", r"\bzoom", False),
    ("control", r"\bpaginat(?:e|ion|ed)\b", "pagination", r"\bpaginat|\bnextPage|\bprevPage", False),
    (
        "control",
        r"\btimer\b|\bcountdown\b|\bstopwatch\b",
        "a timer",
        r"setInterval|setTimeout|\btimer|\bcountdown|\bstopwatch",
        True,
    ),
    # --- capability surfaces -------------------------------------------------------
    ("capability", r"\bcharts?\b|\bgraphs?\b|\bvisuali[sz]", "a chart or graph", r"\bchart|<canvas|<svg|\bgraph", True),
    ("capability", r"\bdark mode\b|\blight mode\b|\btheme\b", "a dark/light theme", r"\bdark|\btheme", True),
    (
        "capability",
        r"\blog ?in\b|\bsign ?in\b|\bsign ?up\b|\bregister\b|\bauth",
        "sign-in / auth",
        r"\bpassword|\blogin|\blog in|\bsign in|\bsignin|\bauth",
        True,
    ),
    (
        "capability",
        r"\bfetch(?:es|ing|ed)?\b|\bAPI\b|\bfrom the server\b",
        "a network fetch",
        r"fetch\(|axios|XMLHttpRequest|\bapi\b",
        True,
    ),
    (
        "capability",
        r"\bforms?\b|\binput fields?\b|\bfields?\b",
        "a form with inputs",
        r"<form|<input|<textarea|<select",
        True,
    ),
    ("capability", r"\bvalidat(?:e|es|ion|ing|ed)\b", "input validation", r"\bvalid|\brequired", False),
    ("capability", r"\bresponsive\b|\bmobile\b|\bphone\b", "a responsive layout", r"@media", True),
    (
        "capability",
        r"\banimat(?:e|es|ion|ed|ing)\b|\btransition",
        "animation or transitions",
        r"@keyframes|transition\s*:|animation\s*:",
        False,
    ),
    ("capability", r"\bkeyboard\b|\bshortcut", "keyboard handling", r"\bkeydown|\bkeyup|\bkeypress", False),
    ("capability", r"\bcanvas\b|\bdraw(?:s|ing|n)?\b", "a canvas surface", r"<canvas|getContext", False),
    ("capability", r"\baudio\b|\bvideo\b|\bplayer\b", "audio or video playback", r"<audio|<video", False),
    ("capability", r"\bmap\b|\bmaps\b", "a map", r"\bmap\b|leaflet|mapbox", False),
    ("capability", r"\bcalendar\b|\bdate ?picker\b", "a calendar or date picker", r"\bcalendar|\bdate", False),
    ("capability", r"\btoast\b|\bnotification", "notifications or toasts", r"\btoast|\bnotification|\balert", False),
    ("capability", r"\bmodal\b|\bdialog\b|\bpopup\b", "a modal or dialog", r"\bmodal|\bdialog", False),
    ("capability", r"\btabs?\b", "tabbed navigation", r"\btab\b|\btabs\b|\btablist\b", False),
    ("capability", r"\baccordion\b|\bcollapsible\b", "an accordion", r"\baccordion|<details", False),
    (
        "capability",
        r"\bcarousel\b|\bslider\b|\bslideshow\b",
        "a carousel or slider",
        r"\bcarousel|\bslider|\bswiper",
        False,
    ),
    ("capability", r"\bprogress\b", "a progress indicator", r"\bprogress", False),
    ("capability", r"\brating\b|\bstars?\b", "a rating control", r"\brating|\bstar", False),
    ("capability", r"\bcomments?\b", "a comment surface", r"\bcomment", False),
    ("capability", r"\bcart\b|\bcheckout\b", "a cart or checkout", r"\bcart|\bcheckout", True),
    ("capability", r"\bquiz\b|\bquestions?\b|\bscore\b", "a quiz or scoring", r"\bquiz|\bquestion|\bscore", False),
    ("capability", r"\bgame\b|\blevels?\b", "game state", r"\bscore|\blevel|\bgame", False),
    ("capability", r"\bto-?do\b|\btasks?\b", "a task list", r"\btodo|\btask|<ul[\s>]|<ol[\s>]|<li[\s>]|\blist\b", True),
    ("capability", r"\bnotes?\b", "a notes surface", r"\bnote", False),
    ("capability", r"\bweather\b|\bforecast\b", "weather data", r"\bweather|\btemperature|\bforecast", False),
    (
        "capability",
        r"\bcurrency\b|\bexchange rate\b",
        "currency conversion",
        r"\bcurrency|\busd|\beur|\bconvert",
        False,
    ),
]

# Content sections a page is expected to have. Matched as whole words against the request;
# a request that names three or more is a real multi-section page, and each one is then a
# hard criterion. One incidental mention stays soft, so a page that merely says "contact"
# in passing is not gated on having a contact section.
SECTION_NOUNS = (
    "hero",
    "about",
    "projects",
    "portfolio",
    "skills",
    "experience",
    "education",
    "contact",
    "pricing",
    "testimonials",
    "features",
    "gallery",
    "blog",
    "faq",
    "services",
    "team",
    "resume",
    "dashboard",
    "settings",
    "profile",
    "cart",
    "checkout",
    "stats",
    "clients",
    "awards",
    "publications",
    "changelog",
    "roadmap",
    "integrations",
    "security",
    "careers",
    "partners",
    "sponsors",
)
SECTION_HARD_AT = 3


def _issue(level: str, code: str, file: str, detail: str) -> dict:
    return {"file": file, "level": level, "code": code, "detail": detail}


def _anchor(files: dict[str, dict]) -> str:
    """The file a whole-build finding is reported against: the page, not a stylesheet."""
    for suffix in (".html", ".htm", ".tsx", ".jsx", ".vue", ".svelte", ".js", ".ts", ".py"):
        for path in sorted(files):
            meta = files.get(path) or {}
            if path.endswith(suffix) and str(meta.get("content") or "").strip():
                return path
    return next(iter(sorted(files)), "index.html")


def _haystack(files: dict[str, dict]):
    """Every written file's text, so evidence is searched across the whole tree."""
    for path in sorted(files):
        content = str((files.get(path) or {}).get("content") or "")
        if content.strip():
            yield path, content


def _satisfied(evidence: str, files: dict[str, dict]) -> str:
    """The first file that contains a trace of this capability, or ''."""
    pattern = re.compile(evidence, re.I)
    for path, content in _haystack(files):
        if pattern.search(content):
            return path
    return ""


def extract(prompt: str) -> list[dict]:
    """The acceptance criteria a request names. Deterministic, offline, no model."""
    text = str(prompt or "")
    if not text.strip():
        return []

    criteria: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for kind, trigger, label, evidence, hard in RULES:
        if not re.search(trigger, text, re.I):
            continue
        key = (kind, label)
        if key in seen:
            continue
        seen.add(key)
        criteria.append(
            {
                "id": f"c{len(criteria) + 1}",
                "kind": kind,
                "label": label,
                "evidence": evidence,
                "hard": hard,
            }
        )

    nouns = [noun for noun in SECTION_NOUNS if re.search(rf"\b{noun}\b", text, re.I)]
    hard_sections = len(nouns) >= SECTION_HARD_AT
    for noun in nouns:
        key = ("section", noun)
        if key in seen:
            continue
        seen.add(key)
        article = "an" if noun[0] in "aeiou" else "a"
        criteria.append(
            {
                "id": f"c{len(criteria) + 1}",
                "kind": "section",
                "label": f"{article} {noun} section",
                "evidence": rf"\b{noun}",
                "hard": hard_sections,
            }
        )

    return criteria[:MAX_CRITERIA]


def coverage(files: dict[str, dict], criteria: list[dict]) -> dict:
    """How much of the request the tree actually contains. Fills `found` on each item."""
    hard = [item for item in criteria if item.get("hard")]
    found_hard = 0
    for item in criteria:
        where = _satisfied(str(item.get("evidence") or ""), files)
        item["found"] = bool(where)
        item["where"] = where
        if where and item.get("hard"):
            found_hard += 1
    return {
        "criteria": len(criteria),
        "hard": len(hard),
        "found": sum(1 for item in criteria if item.get("found")),
        "found_hard": found_hard,
        "ratio": round(found_hard / len(hard), 3) if hard else 1.0,
    }


def check(files: dict[str, dict], criteria: list[dict]) -> list[dict]:
    """Findings for the gap between what was asked for and what was written."""
    if not criteria:
        return []

    stats = coverage(files, criteria)
    anchor = _anchor(files)
    issues: list[dict] = []
    hard = stats["hard"]
    found_hard = stats["found_hard"]

    # The build ignores the request: capabilities were named and none of them landed.
    if hard >= 2 and found_hard == 0:
        named = ", ".join(item["label"] for item in criteria if item.get("hard"))[:300]
        issues.append(
            _issue(
                "error",
                "intent_unmet",
                anchor,
                f"the request named {hard} capability(ies) and the tree contains a trace of none of them "
                f"({named}) — this is not the thing that was asked for",
            )
        )

    # An interactive request produced a static page: no script and no control anywhere.
    controls = [item for item in criteria if item.get("kind") == "control" and item.get("hard")]
    if len(controls) >= 2:
        has_control = any(CONTROL_ELEMENT.search(content) for _, content in _haystack(files))
        has_script = any(
            SCRIPT_ELEMENT.search(content) or path.endswith(SCRIPT_SUFFIXES) for path, content in _haystack(files)
        )
        if not has_control and not has_script:
            issues.append(
                _issue(
                    "error",
                    "intent_static",
                    anchor,
                    f"the request named {len(controls)} interactive action(s) but the tree has no script "
                    "and no button, input, select or form — it is a static document, not an app",
                )
            )

    # Name each gap precisely, so the repair loop has a target rather than a mood.
    missing = [item for item in criteria if not item.get("found")]
    for item in missing[:MAX_GAP_FINDINGS]:
        issues.append(
            _issue(
                "warn",
                "intent_gap",
                anchor,
                f"the request asked for {item['label']} and nothing in the tree mentions it",
            )
        )
    if len(missing) > MAX_GAP_FINDINGS:
        rest = ", ".join(item["label"] for item in missing[MAX_GAP_FINDINGS:])
        issues.append(
            _issue(
                "warn",
                "intent_gap",
                anchor,
                f"{len(missing) - MAX_GAP_FINDINGS} more named capability(ies) are absent: {rest[:240]}",
            )
        )

    # Partial coverage is a warning, never a failure: the run did build something.
    if hard >= 3 and 0 < found_hard < hard / 2:
        issues.append(
            _issue(
                "warn",
                "intent_partial",
                anchor,
                f"only {found_hard} of {hard} named capability(ies) are present — the build covers "
                "less than half of the request",
            )
        )

    return issues


def contract(criteria: list[dict]) -> str:
    """The checklist block injected into every code prompt."""
    if not criteria:
        return "- (the request named no specific capability; build what the objective says)"
    return "\n".join(f"- {item['label']}" for item in criteria)


def summary(criteria: list[dict]) -> dict:
    """A small, stable block for the verify event and the run metrics."""
    return {
        "criteria": len(criteria),
        "hard": sum(1 for item in criteria if item.get("hard")),
        "found": sum(1 for item in criteria if item.get("found")),
        "labels": [item["label"] for item in criteria],
    }


# ---------- the checklist on disk ----------


def _path(run) -> Path | None:
    workspace = getattr(run, "workspace", None)
    return Path(workspace) / FILENAME if workspace is not None else None


def write(run, criteria: list[dict]) -> bool:
    """Persist the checklist beside the run, so a restart and a human can both read it."""
    path = _path(run)
    if path is None or not criteria:
        return False
    lines = [
        f"# Acceptance — {str(getattr(run, 'prompt', ''))[:100]}",
        "",
        "Every line is a capability the request named. The harness checks each one against",
        "the written tree; a line with no trace is a finding.",
        "",
    ]
    for item in criteria:
        mark = "x" if item.get("found") else " "
        hard = "" if item.get("hard") else " (soft)"
        lines.append(f"- [{mark}] {item['label']}{hard}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False
