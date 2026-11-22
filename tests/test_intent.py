"""The acceptance layer: a build is judged against the request, not only itself.

Every other sensor asks whether the tree is consistent with itself — ids match, imports
resolve, the stylesheet matches the markup, the page has a type scale. A weak model can
satisfy all of them and still ship a page with none of the features that were asked for.
These tests pin the missing question, and just as importantly pin that the sensor stays
quiet on a build that did answer the request: a sensor that cries wolf gets ignored.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import intent  # noqa: E402
from ai_harness.nodes import verify  # noqa: E402

# A request that names several distinct capabilities.
ASK = (
    "Build a task tracker with a search box, a filter for status, and a dark mode toggle. "
    "It needs a form to add a task and a button to delete one."
)

# A build that answers the request: every named capability leaves a trace.
ANSWERED = """<!doctype html>
<html lang="en">
  <head><link rel="stylesheet" href="styles.css" /><title>Tasks</title></head>
  <body class="dark">
    <main class="app">
      <h1>Tasks</h1>
      <input id="search" type="search" placeholder="Search tasks" />
      <select id="filter"><option>all</option><option>open</option></select>
      <button id="theme" type="button">Toggle dark mode</button>
      <form id="add"><input id="title" /><button type="submit">Add</button></form>
      <ul id="list"></ul>
    </main>
    <script src="app.js"></script>
  </body>
</html>
"""

ANSWERED_JS = """
const search = document.getElementById("search");
const filter = document.getElementById("filter");
const theme = document.getElementById("theme");
const add = document.getElementById("add");
const list = document.getElementById("list");
theme.addEventListener("click", () => document.body.classList.toggle("dark"));
add.addEventListener("submit", (event) => {
  event.preventDefault();
  const item = document.createElement("li");
  item.textContent = document.getElementById("title").value;
  const remove = document.createElement("button");
  remove.textContent = "Delete";
  remove.addEventListener("click", () => item.remove());
  item.append(remove);
  list.append(item);
});
"""

ANSWERED_CSS = """:root { --ink: #111; --paper: #fff; }
body.dark { --ink: #eee; --paper: #111; }
body { color: var(--ink); background: var(--paper); }
.app { padding: 1rem; }
"""

# A build that is structurally perfect and contains none of the request.
IGNORED = """<!doctype html>
<html lang="en">
  <head><link rel="stylesheet" href="styles.css" /><title>Coming soon</title></head>
  <body>
    <main class="hero"><h1>Coming soon</h1><p class="tagline">Something is on the way.</p></main>
  </body>
</html>
"""

IGNORED_CSS = """:root { --ink: #111; --paper: #fff; --space: 1rem; }
body { color: var(--ink); background: var(--paper); }
.hero { padding: var(--space); }
.tagline { font-size: 1.25rem; }
"""


def _files(**entries: str) -> dict[str, dict]:
    return {path.replace("__", "/"): {"agent": "a", "content": body, "done": True} for path, body in entries.items()}


def _codes(issues: list[dict]) -> set[str]:
    return {item["code"] for item in issues}


def _answered() -> dict[str, dict]:
    return _files(**{"index.html": ANSWERED, "app.js": ANSWERED_JS, "styles.css": ANSWERED_CSS})


def _ignored() -> dict[str, dict]:
    return _files(**{"index.html": IGNORED, "styles.css": IGNORED_CSS})


# ---------- extraction ----------


def test_the_request_becomes_named_criteria():
    criteria = intent.extract(ASK)
    labels = {item["label"] for item in criteria}

    assert "a search control" in labels
    assert "a filter control" in labels
    assert "a dark/light theme" in labels
    assert "an add/create action" in labels
    assert "a delete action" in labels


def test_extraction_is_deterministic_and_offline():
    # Same request, same checklist, every time: no model, no network, no sampling.
    assert intent.extract(ASK) == intent.extract(ASK)


def test_a_request_naming_nothing_specific_yields_no_criteria():
    assert intent.extract("make me a website") == []


def test_an_empty_request_yields_no_criteria():
    assert intent.extract("") == []
    assert intent.extract("   ") == []


def test_section_nouns_become_criteria_only_when_the_page_is_multi_section():
    # One incidental mention is soft; three or more named sections are a real page.
    one = intent.extract("a portfolio with a hero")
    many = intent.extract("a portfolio with a hero, an about section and a contact section")

    assert any(item["kind"] == "section" for item in one)
    assert all(not item["hard"] for item in one if item["kind"] == "section")
    assert any(item["hard"] for item in many if item["kind"] == "section")


# ---------- coverage ----------


def test_a_build_that_answers_the_request_is_fully_covered():
    criteria = intent.extract(ASK)
    stats = intent.coverage(_answered(), criteria)

    assert stats["found"] == stats["criteria"]
    assert stats["ratio"] == 1.0


def test_a_build_that_ignores_the_request_is_not_covered():
    criteria = intent.extract(ASK)
    stats = intent.coverage(_ignored(), criteria)

    assert stats["found_hard"] == 0
    assert stats["ratio"] == 0.0


def test_coverage_records_which_file_satisfied_each_criterion():
    criteria = intent.extract(ASK)
    intent.coverage(_answered(), criteria)

    theme = next(item for item in criteria if "theme" in item["label"])
    assert theme["found"] is True
    assert theme["where"] in {"index.html", "app.js", "styles.css"}


# ---------- findings ----------


def test_ignoring_every_named_capability_is_an_error():
    issues = intent.check(_ignored(), intent.extract(ASK))

    assert "intent_unmet" in _codes(issues)
    assert any(item["level"] == "error" for item in issues)


def test_an_interactive_request_built_as_a_static_page_is_an_error():
    # Two hard controls named, and the tree has no script and no control element.
    request = "a page with a search box and a button to add an item"
    static = _files(**{"index.html": IGNORED, "styles.css": IGNORED_CSS})
    issues = intent.check(static, intent.extract(request))

    assert "intent_static" in _codes(issues)


def test_a_single_missing_capability_is_a_warning_naming_it():
    # Everything but the delete button.
    partial = _files(
        **{"index.html": ANSWERED, "app.js": "const search = 1; const filter = 2;", "styles.css": ANSWERED_CSS}
    )
    issues = intent.check(partial, intent.extract(ASK))

    gaps = [item for item in issues if item["code"] == "intent_gap"]
    assert gaps
    assert all(item["level"] == "warn" for item in gaps)
    assert any("delete" in item["detail"] for item in gaps)


def test_a_build_that_answers_the_request_produces_no_intent_findings():
    assert intent.check(_answered(), intent.extract(ASK)) == []


def test_no_criteria_means_no_findings():
    assert intent.check(_ignored(), []) == []


def test_partial_coverage_is_a_warning_not_a_failure():
    # Half the hard capabilities present: worth reporting, never a failed run by itself.
    request = "a dashboard with a chart, a search box, a filter and a dark mode toggle"
    half = _files(
        **{
            "index.html": '<html><body><input id="search" /><select id="filter"></select></body></html>',
            "app.js": "const q = document.getElementById('search');",
            "styles.css": "body { color: #111; }",
        }
    )
    issues = intent.check(half, intent.extract(request))

    assert "intent_partial" in _codes(issues)
    assert all(item["level"] == "warn" for item in issues if item["code"] == "intent_partial")


# ---------- the verifier tier ----------


def test_the_verifier_reports_intent_findings_alongside_structural_ones():
    report = verify.check(_ignored(), list(_ignored()), intent.extract(ASK))

    assert report["ok"] is False
    assert "intent_unmet" in _codes(report["issues"])


def test_the_verifier_is_unchanged_when_no_checklist_is_passed():
    # Every existing caller — a test, a replayed regression — keeps its old behaviour.
    report = verify.check(_ignored(), list(_ignored()))

    assert "intent_unmet" not in _codes(report["issues"])


def test_a_clean_build_passes_the_verifier_with_a_checklist():
    files = _answered()
    report = verify.check(files, list(files), intent.extract(ASK))

    assert report["ok"] is True


# ---------- the prompt contract ----------


def test_the_contract_lists_every_criterion_for_the_code_prompt():
    block = intent.contract(intent.extract(ASK))

    assert "a search control" in block
    assert "a delete action" in block
    assert block.count("\n") >= 4


def test_the_contract_is_never_empty_so_the_prompt_always_formats():
    # `prompts.CODE.format` needs a value; an empty string would leave a blank section.
    assert intent.contract([]).strip()


# ---------- the checklist on disk ----------


def test_the_checklist_on_disk_tracks_the_tree_not_the_empty_plan(tmp_path: Path):
    """`acceptance.md` must not stay a snapshot of the moment before the build.

    It is written once at planning time, when no file exists and every `found` is False.
    Nothing rewrote it afterwards, so the file a human and the receipt read said "nothing
    was built" on a tree that satisfied most of the request. This drives `verify.run`,
    which is where the rewrite belongs — asserting on `intent.write` alone would pass
    whether or not the verifier ever called it.
    """

    class _Run:
        prompt = ASK
        workspace = tmp_path
        acceptance = None
        planned_files: list[str] = []

        def __init__(self) -> None:
            self.files = _answered()
            self.events: list[dict] = []

        async def emit(self, event: dict) -> None:
            self.events.append(event)

    run = _Run()
    run.acceptance = intent.extract(ASK)

    # Planning time: no files yet, so the checklist is all unticked.
    intent.write(run, run.acceptance)
    before = (tmp_path / intent.FILENAME).read_text()
    assert "- [ ]" in before
    assert "- [x]" not in before

    # A verification pass now that the tree exists must update the file on disk.
    asyncio.run(verify.run(run, "post-build"))

    after = (tmp_path / intent.FILENAME).read_text()
    assert "- [x]" in after, "verify.run did not rewrite the checklist after coverage was known"
    assert after.count("- [x]") >= 3
