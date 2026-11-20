"""Design sensor: a deterministic read of whether a page was designed or only wired.

`nodes/verify.py` proves a page *runs*: ids match, imports resolve, the stylesheet it
links exists. It says nothing about whether the result looks like anything. A model that
emits one `body` rule and a wall of unstyled divs passes every existing check, and the
person who asked for a page gets a document.

This module is the missing sensor. It reads the same files and reports facts about
design: which classes the markup uses that no rule styles, whether the stylesheet has a
type scale, design tokens, a responsive breakpoint or an interactive state, and whether
the stylesheet styles anything the markup actually uses at all.

Severity is chosen so the sensor cannot cry wolf:

* **error** — the page is styled in name only: a stylesheet whose selectors match
  nothing in the markup, or a hand-written HTML page with no styling anywhere.
* **warn** — the page works and is under-designed: unstyled classes, no breakpoint, no
  hover/focus state, no tokens, a flat type scale.

A warning never fails a run. It becomes evidence in the reviewer's prompt and the repair
loop, which is where the harness already turns facts into fixes. A framework tree
(React/Vue/Svelte) is exempt from the hard gate: its stylesheet legitimately arrives
later than its components.
"""

from __future__ import annotations

import re

CLASS_SEL = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")
ID_SEL = re.compile(r"#(-?[_a-zA-Z][\w-]*)")
TAG_SEL = re.compile(r"(?:^|[\s,>+~])([a-zA-Z][\w-]*)")
TAG_IN_MARKUP = re.compile(r"<\s*([a-zA-Z][\w-]*)")
CLASS_ATTR = re.compile(r"""\bclass\s*=\s*["']([^"']*)["']""")
ID_ATTR = re.compile(r"""\bid\s*=\s*["']([^"']*)["']""")
STYLE_ATTR = re.compile(r"""\bstyle\s*=\s*["'][^"']+["']""")
FONT_SIZE = re.compile(r"font-size\s*:\s*([^;}]+)")
TOKEN = re.compile(r"--[\w-]+\s*:")
STATE = re.compile(r":(?:hover|focus|focus-visible|active|disabled)\b")
COMMENT = re.compile(r"/\*.*?\*/", re.S)
FRAMEWORK_SUFFIXES = (".tsx", ".jsx", ".vue", ".svelte")
FRAMEWORK_DEPS = ("react", "vue", "svelte", "vite", "next", "nuxt", "solid-js", "preact")
# Element names that carry no styling intent of their own. `body` is deliberately NOT
# here: styling `body` is the single most common legitimate rule in a stylesheet, and
# treating it as inert made every page that styles only its body read as unstyled.
INERT_TAGS = {"html", "head", "meta", "link", "script", "title", "br", "noscript"}


def _issue(level: str, code: str, file: str, detail: str) -> dict:
    return {"file": file, "level": level, "code": code, "detail": detail}


def _selectors(css: str) -> list[str]:
    """Every selector in the sheet, including the ones nested inside `@media`."""
    found: list[str] = []
    buffer: list[str] = []
    for char in COMMENT.sub("", css):
        if char == "{":
            selector = "".join(buffer).strip()
            if selector and not selector.startswith("@"):
                found.append(selector)
            buffer = []
        elif char == "}":
            buffer = []
        else:
            buffer.append(char)
    return found


def _is_framework(files: dict[str, dict]) -> bool:
    """A component tree may ship its stylesheet after its components; a page may not."""
    if any(path.endswith(FRAMEWORK_SUFFIXES) for path in files):
        return True
    manifest = files.get("package.json", {}).get("content") or ""
    lowered = manifest.lower()
    return any(f'"{dep}"' in lowered for dep in FRAMEWORK_DEPS)


def _styled(selectors: list[str]) -> tuple[set[str], set[str], set[str]]:
    classes: set[str] = set()
    ids: set[str] = set()
    tags: set[str] = set()
    for selector in selectors:
        classes |= set(CLASS_SEL.findall(selector))
        ids |= set(ID_SEL.findall(selector))
        tags |= {tag.lower() for tag in TAG_SEL.findall(selector)}
    return classes, ids, tags


def _audit_page(path: str, html: str, sheets: list[tuple[str, str]], framework: bool) -> list[dict]:
    issues: list[dict] = []
    classes = {name for group in CLASS_ATTR.findall(html) for name in group.split() if name}
    ids = set(ID_ATTR.findall(html))
    tags = {tag.lower() for tag in TAG_IN_MARKUP.findall(html)} - INERT_TAGS
    inline = bool(STYLE_ATTR.search(html))
    style_block = bool(re.search(r"<style\b", html, re.I))

    if not sheets:
        if not style_block and not inline and not framework:
            issues.append(
                _issue(
                    "error",
                    "unstyled_page",
                    path,
                    "the page has no stylesheet, no <style> block and no inline style — "
                    "it renders as unstyled browser defaults",
                )
            )
        return issues

    css = "\n".join(body for _, body in sheets)
    sheet_path = sheets[0][0]
    selectors = _selectors(css)
    if not selectors:
        return issues  # an empty sheet is already an `empty` finding from verify

    styled_classes, styled_ids, styled_tags = _styled(selectors)
    matches = (styled_classes & classes) | (styled_ids & ids) | (styled_tags & tags)
    if not matches:
        issues.append(
            _issue(
                "error",
                "dead_stylesheet",
                sheet_path,
                f"none of its {len(selectors)} selector(s) match a class, id or element in {path} — "
                "the page is styled in name only",
            )
        )
        return issues

    missing = sorted(classes - styled_classes)
    if missing:
        sample = ", ".join(f".{name}" for name in missing[:4])
        more = f" (+{len(missing) - 4} more)" if len(missing) > 4 else ""
        issues.append(
            _issue(
                "warn",
                "unstyled_class",
                sheet_path,
                f"{len(missing)} class(es) used in {path} have no rule: {sample}{more}",
            )
        )

    if "@media" not in css:
        issues.append(
            _issue(
                "warn", "no_responsive", sheet_path, "no @media breakpoint — the layout cannot adapt to a narrow screen"
            )
        )
    if not STATE.search(css):
        issues.append(
            _issue(
                "warn",
                "no_interactive_state",
                sheet_path,
                "no :hover, :focus or :active rule — every control looks inert",
            )
        )
    if not TOKEN.search(css):
        issues.append(
            _issue(
                "warn",
                "no_design_tokens",
                sheet_path,
                "no custom properties — colour, spacing and type cannot be tuned from one place",
            )
        )
    sizes = {value.strip().lower() for value in FONT_SIZE.findall(css)}
    if len(sizes) < 3:
        issues.append(
            _issue(
                "warn",
                "no_type_scale",
                sheet_path,
                f"{len(sizes)} distinct font-size value(s) — a page needs a scale, not one size",
            )
        )
    return issues


def audit(files: dict[str, dict]) -> list[dict]:
    """Design findings for the tree, in the same shape as `nodes/verify` issues."""
    pages = [
        (path, meta.get("content") or "")
        for path, meta in files.items()
        if path.endswith((".html", ".htm")) and (meta.get("content") or "").strip()
    ]
    sheets = [
        (path, meta.get("content") or "")
        for path, meta in files.items()
        if path.endswith((".css", ".scss")) and (meta.get("content") or "").strip()
    ]
    if not pages:
        return []
    framework = _is_framework(files)
    issues: list[dict] = []
    for path, html in pages:
        issues.extend(_audit_page(path, html, sheets, framework))
    return issues


def summary(files: dict[str, dict]) -> dict:
    """A small, stable block for the verify event and the run metrics."""
    issues = audit(files)
    return {
        "errors": sum(1 for item in issues if item["level"] == "error"),
        "warnings": sum(1 for item in issues if item["level"] == "warn"),
        "codes": sorted({item["code"] for item in issues}),
    }
