"""A truncated TSX file must fail before any install exists.

The verifier can read braces and imports, but only a parser knows that `<div><p>hi</p>;`
is not JSX. esbuild is already a Vite dependency, so the harness can ask it for the fact
without running the model's code.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import toolchain  # noqa: E402

pytestmark = pytest.mark.skipif(toolchain._esbuild() is None, reason="esbuild not installed")


def test_truncated_jsx_is_a_syntax_error():
    issue = toolchain.javascript_syntax(
        "src/App.tsx",
        "export default function App() { return (<div><p>hi</p>; }\n",
    )
    assert issue is not None
    assert issue["code"] == "syntax_error"
    assert issue["file"] == "src/App.tsx"


def test_a_valid_component_parses():
    assert (
        toolchain.javascript_syntax(
            "src/App.tsx",
            "export default function App() { return (<div><p>hi</p></div>); }\n",
        )
        is None
    )
