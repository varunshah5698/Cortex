"""Golden cases for the harness eval suite.

Each case is a prompt, the plan a model is scripted to return for it, and the files it
then writes. `expect_ok` is what the deterministic verifier must conclude, so a case can
assert that the harness *detects* a broken build, not only that it can produce a good one.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    name: str
    prompt: str
    plan: dict
    code: dict[str, str]
    expect_ok: bool
    note: str
    # True when this case's tree is meant to run: `--build` serves it through
    # modules/runner.py and fails the case unless the service reaches `ready`.
    # Facts about files are not enough for these (evals/build.py).
    servable: bool = False
    # Issue codes the verifier must report at least once during the run. A case can then
    # assert both halves of the contract: the defect was *seen*, and the harness either
    # repaired it (expect_ok) or refused to report done.
    must_flag: frozenset[str] = field(default_factory=frozenset)

    @property
    def files(self) -> list[str]:
        return [str(path) for path in self.plan.get("files") or []]


def _plan(objective: str, units: list[tuple[str, str, str, str]]) -> dict:
    return {
        "objective": objective,
        "files": [unit[2] for unit in units],
        "work_units": [
            {"title": title, "role": role, "file": path, "objective": objective}
            for title, role, path, objective in units
        ],
        "research_questions": [],
    }


COUNTER_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Counter</title>
    <link rel="stylesheet" href="styles.css" />
  </head>
  <body>
    <main class="card">
      <h1>Counter</h1>
      <p id="count">0</p>
      <button id="go" type="button">Add one</button>
    </main>
    <script src="app.js"></script>
  </body>
</html>
"""

COUNTER_CSS = """body {
  margin: 0;
  font-family: system-ui, sans-serif;
  display: grid;
  place-items: center;
  min-height: 100vh;
}

#count {
  font-size: 3rem;
  margin: 0.5rem 0;
}

#go {
  padding: 0.6rem 1rem;
}
"""

COUNTER_JS = """const count = document.getElementById("count");
const go = document.getElementById("go");

go.addEventListener("click", () => {
  const next = Number(count.textContent || "0") + 1;
  count.textContent = String(next);
});
"""

BROKEN_JS = """// Wires an id the markup never declares: the page loads and does nothing.
const ghost = document.getElementById("ghost");
ghost.textContent = "1";
"""

REACT_MANIFEST = """{
  "name": "counter-app",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build"
  },
  "dependencies": {
    "react": "18.3.1",
    "react-dom": "18.3.1"
  }
}
"""

REACT_INDEX = """<!doctype html>
<html lang="en">
  <head><meta charset="UTF-8" /><title>Counter</title></head>
  <body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body>
</html>
"""

REACT_MAIN = """import { createRoot } from "react-dom/client";
import App from "./App";

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(<App />);
}
"""

REACT_APP = """export default function App() {
  return (
    <main className="card">
      <h1>Counter</h1>
      <p>0</p>
    </main>
  );
}
"""

PYTHON_APP = """def greet(name: str) -> str:
    return f"hello {name}"


def main() -> str:
    return greet("world")


if __name__ == "__main__":
    print(main())
"""

PYTHON_TEST = """from app import greet, main


def test_greet() -> None:
    assert greet("ada") == "hello ada"


def test_main() -> None:
    assert main() == "hello world"
"""

PYTHON_DEPS = "pytest==8.3.3\n"

CASES: list[Case] = [
    Case(
        name="counter-page",
        prompt="build a small counter page with a button and a stylesheet",
        plan=_plan(
            "A one-page counter",
            [
                ("Markup", "frontend", "index.html", "the counter markup, linking styles.css and app.js"),
                ("Styling", "frontend", "styles.css", "a calm centered card"),
                ("Wiring", "backend", "app.js", "increment #count when #go is clicked"),
            ],
        ),
        code={"index.html": COUNTER_HTML, "styles.css": COUNTER_CSS, "app.js": COUNTER_JS},
        expect_ok=True,
        note="the baseline: a consistent trio must pass verification",
        servable=True,
    ),
    Case(
        name="broken-wiring",
        prompt="build a counter page but the script queries an id the markup lacks",
        plan=_plan(
            "A counter with a wiring defect",
            [
                ("Markup", "frontend", "index.html", "the counter markup"),
                ("Styling", "frontend", "styles.css", "the card styling"),
                ("Wiring", "backend", "app.js", "wire the button"),
            ],
        ),
        code={"index.html": COUNTER_HTML, "styles.css": COUNTER_CSS, "app.js": BROKEN_JS},
        # A script for another page is not this page's script: `nodes/review.wire` repairs
        # renamed ids, but it will not invent a DOM for an id nothing token-matches. So the
        # honest contract is detection plus refusal — `must_flag` requires the sensor to
        # name the defect, and `expect_ok=False` requires the run to stop short on it.
        expect_ok=False,
        must_flag=frozenset({"missing_id"}),
        note="the verifier must report the missing id and the run must refuse to finish on it",
    ),
    Case(
        name="react-tree",
        prompt="build a react app with vite and typescript",
        plan=_plan(
            "A minimal React app",
            [
                ("Manifest", "backend", "package.json", "name, scripts and react dependencies"),
                ("Shell", "frontend", "index.html", "the document that mounts #root"),
                ("Entry", "frontend", "src/main.tsx", "mount App into #root"),
                ("UI", "frontend", "src/App.tsx", "the component tree"),
            ],
        ),
        code={
            "package.json": REACT_MANIFEST,
            "index.html": REACT_INDEX,
            "src/main.tsx": REACT_MAIN,
            "src/App.tsx": REACT_APP,
        },
        expect_ok=True,
        note="a stack-aware scaffold whose imports resolve",
        servable=True,
    ),
    Case(
        name="python-service",
        prompt="build a small python module with a test",
        plan=_plan(
            "A tested Python module",
            [
                ("Application", "backend", "app.py", "the runnable program"),
                ("Dependencies", "backend", "requirements.txt", "pinned packages"),
                ("Checks", "general", "test_app.py", "tests for greet and main"),
            ],
        ),
        code={"app.py": PYTHON_APP, "requirements.txt": PYTHON_DEPS, "test_app.py": PYTHON_TEST},
        expect_ok=True,
        note="a Python tree whose local import resolves",
    ),
    Case(
        name="missing-file",
        prompt="build a counter page but never write the stylesheet",
        plan=_plan(
            "A counter with a missing stylesheet",
            [
                ("Markup", "frontend", "index.html", "the counter markup"),
                ("Styling", "frontend", "styles.css", "the card styling"),
                ("Wiring", "backend", "app.js", "wire the button"),
            ],
        ),
        code={"index.html": COUNTER_HTML, "app.js": COUNTER_JS},
        expect_ok=False,
        note="a declared file nobody wrote must be reported as empty",
    ),
]
