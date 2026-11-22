"""The design sensor: a page is judged as a designed artefact, not only a wired one.

`nodes/verify` proves a page runs. These tests pin the second question — was it designed —
and, just as importantly, that the sensor stays quiet on a page that is fine. A sensor
that cries wolf gets ignored, and an ignored sensor is worse than none.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import design  # noqa: E402
from ai_harness.nodes import verify  # noqa: E402

PAGE = """<!doctype html>
<html lang="en">
  <head><link rel="stylesheet" href="styles.css" /></head>
  <body>
    <main class="card">
      <h1 class="title">Counter</h1>
      <p id="count">0</p>
      <button id="go" class="primary" type="button">Add one</button>
    </main>
    <script src="app.js"></script>
  </body>
</html>
"""

DESIGNED_CSS = """:root {
  --ink: #16181d;
  --accent: #2f6feb;
  --space-2: 0.5rem;
  --space-4: 1rem;
  --text-sm: 0.875rem;
  --text-lg: 1.5rem;
  --text-xl: 2.5rem;
}

body {
  font-size: var(--text-sm);
  color: var(--ink);
}

.card {
  padding: var(--space-4);
}

.title {
  font-size: var(--text-lg);
}

#count {
  font-size: var(--text-xl);
}

.primary {
  background: var(--accent);
  padding: var(--space-2) var(--space-4);
}

.primary:hover {
  filter: brightness(1.1);
}

.primary:focus-visible {
  outline: 2px solid var(--accent);
}

@media (max-width: 640px) {
  .card {
    padding: var(--space-2);
  }
}
"""

# Wired correctly, styled in name only: nothing in the sheet matches the markup.
DEAD_CSS = """.hero-banner {
  padding: 4rem;
}

.pricing-grid {
  display: grid;
}
"""

BARE_PAGE = """<!doctype html>
<html lang="en">
  <head><title>Counter</title></head>
  <body>
    <main><h1>Counter</h1><p id="count">0</p><button id="go">Add one</button></main>
    <script src="app.js"></script>
  </body>
</html>
"""


def _files(**entries: str) -> dict[str, dict]:
    return {path.replace("__", "/"): {"agent": "a", "content": body, "done": True} for path, body in entries.items()}


def _codes(issues: list[dict]) -> set[str]:
    return {item["code"] for item in issues}


def test_a_designed_page_produces_no_design_findings():
    files = _files(**{"index.html": PAGE, "styles.css": DESIGNED_CSS})

    assert design.audit(files) == []


def test_a_stylesheet_that_matches_nothing_is_an_error_not_a_warning():
    files = _files(**{"index.html": PAGE, "styles.css": DEAD_CSS})
    issues = design.audit(files)

    dead = [item for item in issues if item["code"] == "dead_stylesheet"]
    assert dead and dead[0]["level"] == "error"
    assert dead[0]["file"] == "styles.css"


def test_a_page_with_no_styling_at_all_is_an_error():
    files = _files(**{"index.html": BARE_PAGE})
    issues = design.audit(files)

    assert "unstyled_page" in _codes(issues)
    assert all(item["level"] == "error" for item in issues)


def test_an_inline_style_attribute_counts_as_some_styling():
    page = BARE_PAGE.replace("<main>", '<main style="padding: 2rem">')
    issues = design.audit(_files(**{"index.html": page}))

    assert "unstyled_page" not in _codes(issues)


def test_under_design_is_a_warning_and_never_fails_the_run():
    # Matches the markup, but has one size, no tokens, no breakpoint and no states.
    thin = ".card { padding: 1rem; }\n.title { font-size: 1rem; }\n"
    files = _files(**{"index.html": PAGE, "styles.css": thin})
    issues = design.audit(files)

    assert {"no_responsive", "no_interactive_state", "no_design_tokens", "no_type_scale"} <= _codes(issues)
    assert all(item["level"] == "warn" for item in issues)


def test_unstyled_classes_are_named_so_the_fix_is_obvious():
    # Drop every rule that mentions .primary, including its :hover and :focus-visible.
    partial = "\n".join(block for block in DESIGNED_CSS.split("\n\n") if ".primary" not in block)
    assert ".primary" not in partial
    issues = design.audit(_files(**{"index.html": PAGE, "styles.css": partial}))

    unstyled = [item for item in issues if item["code"] == "unstyled_class"]
    assert unstyled and ".primary" in unstyled[0]["detail"]


def test_a_framework_tree_is_not_gated_on_a_stylesheet_that_arrives_later():
    # A React tree legitimately ships components before the sheet that styles them.
    files = _files(**{"index.html": BARE_PAGE, "src__App.tsx": "export default function App() { return null; }"})

    assert design.audit(files) == []


def test_the_verifier_reports_design_findings_alongside_wiring_findings():
    files = _files(
        **{"index.html": PAGE, "styles.css": DEAD_CSS, "app.js": 'const el = document.getElementById("count");\n'}
    )
    report = verify.check(files, list(files))

    assert report["ok"] is False
    assert "dead_stylesheet" in _codes(report["issues"])


def test_design_warnings_alone_do_not_fail_verification():
    thin = ".card { padding: 1rem; }\n.title { font-size: 1rem; }\n"
    # app.js is written too, so the only findings left are the design warnings.
    files = _files(
        **{"index.html": PAGE, "styles.css": thin, "app.js": 'const el = document.getElementById("count");\n'}
    )
    report = verify.check(files, list(files))

    assert report["ok"] is True
    assert report["warnings"] > 0


# ---------- regressions: false positives that would block a real run ----------
#
# Each of these was a real defect found by adversarial review. A sensor that fails a
# correct page is worse than no sensor, because it teaches the model to fear the check.

DESIGNED_SHEET = """:root{--ink:#111;--a:0.875rem;--b:1.5rem;--c:2.5rem}
.card{padding:1rem}
.card:hover{filter:brightness(1.1)}
.card:focus-visible{outline:2px solid #111}
@media (max-width:640px){.card{padding:.5rem}}
"""


def test_the_harness_own_react_scaffold_is_not_failed_by_a_component_sheet():
    """The scaffold emits <div id="root">; a component-scoped sheet is the correct answer.

    The framework exemption was applied to `unstyled_page` but not to `dead_stylesheet`,
    so a correct React app failed the hard gate and could never reach `done`.
    """
    from ai_harness.nodes import planning

    html = planning._index("src/main.tsx", "src/index.css")
    files = _files(
        **{
            "index.html": html,
            "src__index.css": DESIGNED_SHEET,
            "src__main.tsx": 'import App from "./App";',
            "src__App.tsx": "export default function App(){return <main className='card'>x</main>}",
        }
    )

    # The intent is that a correct React tree is not *failed*: no error-level finding.
    # Warnings about the sheet's own polish (a type scale) are legitimate and were always
    # reported — this test used to pass only because the sensor never read the component,
    # so it never saw that the sheet was the only thing being judged.
    issues = design.audit(files)
    assert [item for item in issues if item["level"] == "error"] == []
    assert "dead_stylesheet" not in _codes(issues)
    assert "unstyled_class" not in _codes(issues)


def test_a_style_block_plus_a_reset_sheet_is_styled():
    page = (
        '<html><head><link rel="stylesheet" href="reset.css"><style>'
        + DESIGNED_SHEET
        + '</style></head><body><main class="card">x</main></body></html>'
    )
    issues = design.audit(_files(**{"index.html": page, "reset.css": "*{box-sizing:border-box}"}))

    assert "dead_stylesheet" not in _codes(issues)


def test_a_page_is_audited_against_its_own_sheet_not_every_sheet():
    """Blame the dead sheet, not the good one — the repair loop edits what it is told."""
    a = '<html><head><link rel="stylesheet" href="a.css"></head><body><main class="card">a</main></body></html>'
    b = '<html><head><link rel="stylesheet" href="b.css"></head><body><main class="card">b</main></body></html>'
    files = _files(**{"a.html": a, "b.html": b, "a.css": ".card{padding:1rem}", "b.css": ".ghost{x:1}"})

    dead = [item for item in design.audit(files) if item["code"] == "dead_stylesheet"]
    assert dead and dead[0]["file"] == "b.css"


def test_unquoted_and_uppercase_class_attributes_are_read():
    for markup in ("<main class=card>x</main>", '<main CLASS="card">x</main>'):
        page = f'<html><head><link rel="stylesheet" href="s.css"></head><body>{markup}</body></html>'
        issues = design.audit(_files(**{"index.html": page, "s.css": ".card{padding:1rem}"}))
        assert "dead_stylesheet" not in _codes(issues), markup


def test_an_external_stylesheet_counts_as_styled():
    page = (
        '<html><head><link rel="stylesheet" '
        'href="https://cdn.jsdelivr.net/npm/bootstrap/dist/css/bootstrap.min.css">'
        '</head><body><main class="card">x</main></body></html>'
    )

    assert "unstyled_page" not in _codes(design.audit(_files(**{"index.html": page})))


def test_a_brace_inside_a_css_string_cannot_rescue_a_dead_sheet():
    """`content: ".card{"` used to invent a `.card` selector and pass a dead sheet."""
    tricky = '.hero::after{content:".card{"}\n.pricing{display:grid}'
    page = '<html><head><link rel="stylesheet" href="s.css"></head><body><main class="card">x</main></body></html>'
    issues = design.audit(_files(**{"index.html": page, "s.css": tricky}))

    assert "dead_stylesheet" in _codes(issues)


def test_a_commented_out_breakpoint_is_not_a_breakpoint():
    sheet = ".card{padding:1rem}\n/* @media (max-width:640px){.card{padding:.5rem}} */\n"
    page = '<html><head><link rel="stylesheet" href="s.css"></head><body><main class="card">x</main></body></html>'
    issues = design.audit(_files(**{"index.html": page, "s.css": sheet}))

    assert "no_responsive" in _codes(issues)


def test_a_bad_timeout_env_does_not_stop_the_harness_importing():
    """The mirror may never break the process it only observes."""
    import importlib
    import os
    import subprocess
    import sys

    env = {**os.environ, "CONVEX_SYNC_TIMEOUT": "not-a-number", "PYTHONPATH": "src"}
    result = subprocess.run(
        [sys.executable, "-c", "import ai_harness.main"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    assert result.returncode == 0, result.stderr[-500:]
    importlib.invalidate_caches()


def test_a_react_component_using_a_class_the_sheet_never_defines_is_reported():
    """JSX writes `className`, and the sensor only matched `class`.

    A real build shipped `.btn`, `.hero-actions` and `.lead` in its components while
    `index.css` defined none of them: the buttons rendered as bare underlined links with no
    gap between them. The sensor reported nothing, because `CLASS_ATTR` could not see a
    single React class and the page's own `<div id="root">` carries none either.
    """
    from ai_harness.nodes import planning

    html = planning._index("src/main.tsx", "src/index.css")
    files = _files(
        **{
            "index.html": html,
            "src__index.css": DESIGNED_SHEET,
            "src__main.tsx": 'import App from "./App";',
            "src__App.tsx": (
                "export default function App(){return <section className='hero'><a className='btn'>Go</a></section>}"
            ),
        }
    )

    codes = _codes(design.audit(files))
    assert "unstyled_class" in codes, "a component class with no rule must be reported"


def test_a_react_component_whose_classes_are_all_defined_is_quiet():
    """The sensor must not cry wolf on a tree that is actually styled."""
    from ai_harness.nodes import planning

    html = planning._index("src/main.tsx", "src/index.css")
    files = _files(
        **{
            "index.html": html,
            "src__index.css": DESIGNED_SHEET,
            "src__main.tsx": 'import App from "./App";',
            "src__App.tsx": "export default function App(){return <main className='card'>x</main>}",
        }
    )

    assert "unstyled_class" not in _codes(design.audit(files))
