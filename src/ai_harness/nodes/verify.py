"""Verification node: a sensor that does not ask a model for its opinion.

The reviewer is a model reading the work of other models, and it says "APPROVED" more
often than it should. These checks are deterministic: they read the artefacts the run
actually wrote and report contradictions that make the result dead — a script wiring an
id no element has, a stylesheet the markup links but nobody created, an empty declared
file, a stream that stopped mid-file.

Checks are chosen by what a file *is*, not by which stack the plan named, so a React,
Python, Go or Rust build gets the same class of sensor as a web page:

===============  ==================================================================
markup           missing asset, dead id wiring, prose in a .html file
every file       empty, placeholder text, unbalanced braces
*.json           must parse (comments tolerated, as tsconfig allows them)
*.ts/.tsx/.js…   relative imports must resolve to a file this run wrote
*.py             relative imports must resolve to a module this run wrote
*.go             imports under the go.mod module must resolve to a written package
*.rs             `mod x;` must resolve to x.rs or x/mod.rs
===============  ==================================================================

Deliberate limit: only *relative* JS/Python imports are checked. A bare specifier
(`react`, `fastapi`, `fmt`) may be third-party, stdlib or a package the run was never
going to write, so guessing there would produce noise instead of facts.

Findings are fed to the reviewer as evidence and recorded in the run metrics, so a
broken build cannot pass review silently.
"""

from __future__ import annotations

import json
import posixpath
import re

from ..core.state import RunState
from ..modules import design, toolchain

ID_IN_HTML = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""")
REF_IN_HTML = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""")
GET_BY_ID = re.compile(r"""getElementById\(\s*["']([^"']+)["']\s*\)""")
QUERY_ID = re.compile(r"""querySelector(?:All)?\(\s*["']#([\w-]+)["']""")
SCRIPT_TAG = re.compile(r"<script\b", re.I)
STYLESHEET_LINK = re.compile(r"""<link\b[^>]*\brel\s*=\s*["']?stylesheet""", re.I)
PLACEHOLDERS = ("todo:", "fixme", "your code here", "lorem ipsum")
EXTERNAL = ("http://", "https://", "//", "data:", "mailto:", "#")
# A file marker that leaked into the file itself. `>>>` alone is the tail of
# `<<<FILE path>>>`; JS's `a >>> b` is not a whole line and is untouched.
MARKER_ARTIFACT = re.compile(r"^\s*(?:>>>|<<<FILE\b.*>>>)\s*$", re.M)

# Anything that can query the DOM. Scaffolding emits .tsx/.ts now, not just .js.
SCRIPT_SUFFIXES = (".js", ".mjs", ".jsx", ".ts", ".tsx")
MODULE_SUFFIXES = SCRIPT_SUFFIXES + (".vue", ".svelte")
# Extensions an extensionless relative import may resolve to.
RESOLVABLE_EXTENSIONS = MODULE_SUFFIXES + (".json", ".css")
INDEX_FILES = ("index.ts", "index.tsx", "index.js", "index.jsx")
ENTRY_FILES = ("src/main.tsx", "src/main.jsx", "src/main.ts", "src/main.js")
ROOT_COMPONENTS = ("src/App.tsx", "src/App.jsx", "src/App.vue", "src/App.svelte")

JS_FROM = re.compile(r"""\bfrom\s*["']([^"']+)["']""")
JS_REQUIRE = re.compile(r"""\brequire\(\s*["']([^"']+)["']\s*\)""")
JS_SIDE_EFFECT = re.compile(r"""\bimport\s*["']([^"']+)["']""")
PY_FROM = re.compile(r"""^\s*from\s+(\.+)([\w.]*)\s+import\s+([^\n#]+)""", re.M)
GO_BLOCK = re.compile(r"""import\s*\(([^)]*)\)""", re.S)
GO_SINGLE = re.compile(r"""^\s*import\s+(?:[\w.]+\s+)?"([^"]+)"\s*$""", re.M)
GO_QUOTED = re.compile(r"""(?:[\w.]+\s+)?"([^"]+)\"""")
GO_MODULE = re.compile(r"^\s*module\s+(\S+)", re.M)
RS_MOD = re.compile(r"""^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;""", re.M)


def _issue(bucket: list[dict], level: str, code: str, file: str, detail: str) -> None:
    entry = {"file": file, "level": level, "code": code, "detail": detail}
    if entry not in bucket:
        bucket.append(entry)


def _basenames(files: dict[str, dict]) -> set[str]:
    return {path.split("/")[-1] for path in files}


def _norm(path: str) -> str:
    """Normalise a run-relative path; anything escaping the run has no resolution."""
    return posixpath.normpath(path)


def _strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments outside strings, so a commented tsconfig parses."""
    out: list[str] = []
    index, length = 0, len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _resolve_js(base: str, spec: str, files: dict[str, dict]) -> str | None:
    target = _norm(posixpath.join(base, spec))
    if target.startswith(".."):
        return None
    candidates = [target]
    candidates.extend(target + extension for extension in RESOLVABLE_EXTENSIONS)
    candidates.extend(posixpath.join(target, index) for index in INDEX_FILES)
    return next((candidate for candidate in candidates if candidate in files), None)


def _py_exists(target: str, files: dict[str, dict]) -> bool:
    return bool(target) and (f"{target}.py" in files or f"{target}/__init__.py" in files)


def _import_names(raw: str) -> list[str]:
    names: list[str] = []
    for part in raw.replace("(", " ").replace(")", " ").split(","):
        name = part.strip().split(" ")[0].strip()
        if name and name != "*" and name not in names:
            names.append(name)
    return names


def _check_imports(path: str, content: str, files: dict[str, dict], issues: list[dict]) -> None:
    base = posixpath.dirname(path)

    if path.endswith(MODULE_SUFFIXES):
        specifiers = (
            set(JS_FROM.findall(content)) | set(JS_REQUIRE.findall(content)) | set(JS_SIDE_EFFECT.findall(content))
        )
        for spec in sorted(specifiers):
            if not spec.startswith("."):
                continue  # bare specifier: a package, not a file this run was meant to write
            if _resolve_js(base, spec, files) is None:
                _issue(issues, "error", "unresolved_import", path, f"imports {spec!r} but this run wrote no such file")
        return

    if path.endswith(".py"):
        for dots, module, names in PY_FROM.findall(content):
            root = base
            for _ in range(len(dots) - 1):
                root = posixpath.dirname(root)
            if module:
                target = _norm(posixpath.join(root, module.replace(".", "/")))
                if not _py_exists(target, files):
                    _issue(
                        issues,
                        "error",
                        "unresolved_import",
                        path,
                        f"imports from {dots}{module} but no {target or '.'}.py was written",
                    )
                continue
            for name in _import_names(names):
                target = _norm(posixpath.join(root, name))
                if not _py_exists(target, files):
                    _issue(
                        issues,
                        "error",
                        "unresolved_import",
                        path,
                        f"imports {name!r} from its own package but no module by that name was written",
                    )


def _check_go(path: str, content: str, module_path: str, files: dict[str, dict], issues: list[dict]) -> None:
    if not module_path:
        return
    specifiers = set(GO_SINGLE.findall(content))
    for block in GO_BLOCK.findall(content):
        specifiers |= set(GO_QUOTED.findall(block))
    for spec in sorted(specifiers):
        if spec != module_path and not spec.startswith(module_path + "/"):
            continue  # stdlib or third-party
        relative = spec[len(module_path) :].lstrip("/")
        if not relative:
            continue
        if not any(other == relative or other.startswith(relative + "/") for other in files):
            _issue(
                issues,
                "error",
                "unresolved_import",
                path,
                f'imports "{spec}" but this run wrote nothing under {relative}/',
            )


def _check_rust(path: str, content: str, files: dict[str, dict], issues: list[dict]) -> None:
    base = posixpath.dirname(path)
    for module in RS_MOD.findall(content):
        candidates = (_norm(posixpath.join(base, f"{module}.rs")), _norm(posixpath.join(base, module, "mod.rs")))
        if not any(candidate in files for candidate in candidates):
            _issue(
                issues,
                "error",
                "unresolved_import",
                path,
                f"declares `mod {module};` but wrote neither {candidates[0]} nor {candidates[1]}",
            )


def _check_manifest(path: str, data: dict, files: dict[str, dict], issues: list[dict]) -> None:
    """The manifest and the tree must describe the same build."""
    if not isinstance(data, dict):
        return
    scripts = data.get("scripts") or {}
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    runs = " ".join(str(value).lower() for value in scripts.values())
    names = set(files)
    nextish = "next" in deps or "next " in runs or 'next"' in runs
    viteish = "vite" in deps or "vite" in runs
    has_vite_tree = any(name in names for name in ("src/main.tsx", "src/main.jsx", "index.html"))
    has_next_tree = any(name == "next.config.js" or name.startswith(("app/", "pages/")) for name in names)

    if nextish and has_vite_tree and not has_next_tree:
        _issue(
            issues,
            "error",
            "manifest_mismatch",
            path,
            "declares Next.js but the tree is a Vite entry (src/main.tsx/index.html); "
            "use Vite scripts or plan the Next app/ or pages/ tree",
        )
    if viteish and "index.html" not in names:
        _issue(
            issues,
            "error",
            "missing_entry",
            path,
            "runs Vite but the tree has no index.html for Vite to serve",
        )
    if "src/main.tsx" in names and "index.html" not in names and not nextish:
        _issue(
            issues,
            "error",
            "missing_entry",
            "src/main.tsx",
            "a Vite/React entry mounts #root, so the tree needs index.html",
        )
    if "src/main.ts" in names and "index.html" not in names and not nextish:
        _issue(
            issues,
            "error",
            "missing_entry",
            "src/main.ts",
            "a Vite entry mounts an app, so the tree needs index.html",
        )


def _check_root_component(files: dict[str, dict], issues: list[dict]) -> None:
    """The entry must render the root component, not re-implement it beside it.

    An agent that owns `src/main.tsx` but not `src/App.tsx` used to write the whole page
    into the entry, leaving App unreferenced. Both files then looked valid in isolation.
    """
    if not any(path in files for path in ENTRY_FILES):
        return
    for app in ROOT_COMPONENTS:
        meta = files.get(app) or {}
        if not (meta.get("content") or "").strip():
            continue
        stem = posixpath.splitext(app.rsplit("/", 1)[-1])[0]
        pattern = re.compile(rf"""(?:\bfrom\s*|\bimport\s+)["'][^"']*\b{re.escape(stem)}(?:\.\w+)?["']""")
        imported = any(
            other != app and pattern.search(meta.get("content") or "")
            for other, meta in files.items()
            if (meta.get("content") or "").strip()
        )
        if not imported:
            _issue(
                issues,
                "error",
                "unreferenced_root",
                app,
                f"{app} is not imported by any other file — the entry must render it, not duplicate it",
            )


def check(files: dict[str, dict], declared: list[str] | None = None) -> dict:
    """Read the written tree and report contradictions. No model, no network."""
    declared = list(declared or files.keys())
    names = _basenames(files)
    issues: list[dict] = []

    for path in declared:
        meta = files.get(path) or {}
        content = meta.get("content", "") or ""
        if not content.strip():
            _issue(issues, "error", "empty", path, f"{path} was declared by the plan but is still empty")
            continue
        lowered = content.lower()
        for marker in PLACEHOLDERS:
            if marker in lowered:
                _issue(issues, "warn", "placeholder", path, f"still contains {marker!r}")
        leak = MARKER_ARTIFACT.search(content)
        if leak:
            _issue(
                issues,
                "error",
                "stray_marker",
                path,
                f"contains the model protocol marker {leak.group(0).strip()!r}; remove it",
            )
        if path.endswith((".css",) + SCRIPT_SUFFIXES):
            if content.count("{") != content.count("}"):
                _issue(
                    issues,
                    "warn",
                    "unbalanced_braces",
                    path,
                    f"unbalanced braces: {content.count('{')} open, {content.count('}')} close",
                )

    html_path = next(
        (p for p in files if p.endswith((".html", ".htm")) and (files[p].get("content") or "").strip()), None
    )
    if html_path:
        html = files[html_path].get("content", "")
        ids = set(ID_IN_HTML.findall(html))
        lowered = html.lower()
        if "<html" not in lowered and "<body" not in lowered:
            _issue(issues, "error", "not_html", html_path, "no <html> or <body> element")

        for ref in REF_IN_HTML.findall(html):
            target = ref.strip()
            if not target or target.lower().startswith(EXTERNAL):
                continue
            local = target.lstrip("./")
            if local.endswith((".css", ".js")) and local.split("/")[-1] not in names:
                _issue(issues, "error", "missing_asset", html_path, f"links {target} but no such file was written")

        loads_a_script = bool(SCRIPT_TAG.search(html))
        for script_path, meta in files.items():
            if not script_path.endswith(SCRIPT_SUFFIXES):
                continue
            script = meta.get("content", "") or ""
            wanted = set(GET_BY_ID.findall(script)) | set(QUERY_ID.findall(script))
            if not loads_a_script and wanted & ids:
                # The script drives ids only this markup declares, yet the markup never
                # loads it: the page renders but nothing is wired. One issue per script,
                # named on the markup, because adding the tag is a markup fix.
                _issue(
                    issues,
                    "error",
                    "unloaded_script",
                    html_path,
                    f"never loads {script_path}, which drives #{sorted(wanted & ids)[0]} — the page cannot run",
                )
                loads_a_script = True
            for wanted_id in sorted(wanted):
                if wanted_id not in ids:
                    # Blame the file that is actually wrong: the script that queries a
                    # missing id, not the markup it queries. The markup is named in the
                    # detail so the fix is still obvious.
                    _issue(
                        issues,
                        "error",
                        "missing_id",
                        script_path,
                        f"queries '#{wanted_id}' but {html_path} declares no element with that id",
                    )

    # Per-stack wiring: manifests, modules and packages the run claims to have.
    go_module = ""
    go_mod = files.get("go.mod", {}).get("content", "") or ""
    module_match = GO_MODULE.search(go_mod)
    if module_match:
        go_module = module_match.group(1)

    for path, meta in files.items():
        content = meta.get("content", "") or ""
        if not content.strip():
            continue
        if path.endswith(".json"):
            try:
                data = json.loads(_strip_jsonc(content))
            except json.JSONDecodeError as exc:
                _issue(
                    issues,
                    "error",
                    "invalid_json",
                    path,
                    f"does not parse as JSON ({exc.msg} at line {exc.lineno})",
                )
            else:
                if path.rsplit("/", 1)[-1] == "package.json":
                    _check_manifest(path, data, files, issues)
        if path.endswith(MODULE_SUFFIXES + (".py",)):
            _check_imports(path, content, files, issues)
        if path.endswith(".rs"):
            _check_rust(path, content, files, issues)
        if path.endswith(".go"):
            _check_go(path, content, go_module, files, issues)

    _check_root_component(files, issues)

    # A Python file that does not parse is a fact about its text, so it belongs with the
    # structural checks rather than in the toolchain pass that needs a subprocess.
    for item in toolchain.syntax_issues(files):
        _issue(issues, "error", item["code"], item["file"], item["detail"])

    # Tier 1.5: was the page designed, or only wired? Same evidence, a different question,
    # and the only one that can tell a page from a document. Errors here mean the styling
    # is decorative (a sheet nothing matches, a page with none); warnings mean it works and
    # is under-designed, which is evidence for the reviewer rather than a failed run.
    for item in design.audit(files):
        _issue(issues, item["level"], item["code"], item["file"], item["detail"])

    return _report(issues, len(declared))


def _report(issues: list[dict], checked: int) -> dict:
    errors = sum(1 for item in issues if item["level"] == "error")
    return {
        "ok": errors == 0,
        "issues": issues,
        "errors": errors,
        "warnings": len(issues) - errors,
        "checked": checked,
    }


def merge(report: dict, issues: list[dict]) -> dict:
    """Fold extra findings into a report, keeping one entry per distinct issue."""
    merged = list(report.get("issues") or [])
    for item in issues:
        if item not in merged:
            merged.append(item)
    return {**_report(merged, int(report.get("checked", 0))), "stage": report.get("stage")}


def as_findings(report: dict) -> str:
    """Render issues as prompt evidence for the reviewer."""
    if not report.get("issues"):
        return ""
    lines = ["DETERMINISTIC CHECKS (run by the harness, not by a model):"]
    for item in report["issues"][:12]:
        lines.append(f"- [{item['level']}] {item['file']} · {item['code']}: {item['detail']}")
    lines.append("These are facts about the files. Report and fix them before anything cosmetic.")
    return "\n".join(lines)


async def run(state: RunState, stage: str = "post-build") -> dict:
    declared = list(dict.fromkeys([*state.files.keys(), *(getattr(state, "planned_files", []) or [])]))
    report = check(state.files, declared)
    report["stage"] = stage
    # Tier 2: the artifact's own compiler, when the tree has one installed. It only adds
    # facts (a type error, an unparseable file), so it runs on every structural pass.
    # A caller with only a file map (a test, a replayed regression) has no workspace, and
    # the syntax half already ran inside `check`.
    workspace = getattr(state, "workspace", None)
    extra = (
        await toolchain.check(workspace, state.files)
        if workspace is not None
        else {"issues": [], "checked": False, "note": "no workspace to type-check"}
    )
    if extra["issues"]:
        report = merge(report, extra["issues"])
        report["stage"] = stage
    report["toolchain"] = {"checked": extra["checked"], "note": extra["note"]}
    await state.emit(
        {
            "type": "verify",
            "stage": stage,
            "ok": report["ok"],
            "issues": report["issues"][:12],
            "errors": report["errors"],
            "warnings": report["warnings"],
            "checked": report["checked"],
            "toolchain": report.get("toolchain"),
        }
    )
    verdict = "clean" if report["ok"] else f"{report['errors']} error(s)"
    await state.emit(
        {
            "type": "message",
            "from": "verify",
            "text": f"{stage} checks: {report['checked']} files, {verdict}, {report['warnings']} warning(s)",
            "kind": "review",
        }
    )
    return report
