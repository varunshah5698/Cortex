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
