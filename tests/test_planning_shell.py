"""The harness owns the Vite shell, including the preview's base path.

The preview serves a run under a per-run token prefix. Absolute `/assets/...` URLs then
404, and the build renders a blank page even though `vite build` succeeded.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import Specialist, WorkUnit  # noqa: E402
from ai_harness.nodes import planning  # noqa: E402


def _spec(spec_id: str, role: str) -> Specialist:
    return Specialist(
        id=spec_id, name=spec_id.title(), role=role, kind="openai", baseUrl="https://api.example/v1", model="m"
    )


def _unit(index: int) -> WorkUnit:
    return WorkUnit(id=f"u{index}", title="file", role="frontend", file=f"src/F{index}.tsx", objective="render it")


def test_the_reviewer_is_not_assigned_a_file_unless_nobody_else_can_write():
    roster = [_spec("atlas", "planner"), _spec("vega", "frontend"), _spec("lyra", "reviewer")]
    mapping = planning.assign(roster, [_unit(index) for index in range(6)])

    assert all(spec.id != "lyra" for spec in mapping.values())
    assert planning.assign([_spec("lyra", "reviewer")], [_unit(0)])["u0"].id == "lyra"


def test_the_vite_shell_uses_relative_assets_and_a_real_entry():
    assert 'base: "./"' in planning._vite_config()
    html = planning._index("src/main.tsx", "src/index.css")
    assert "/src/main.tsx" in html and 'href="/src/index.css"' in html
    assert "--accent" in planning._base_css()
    manifest = planning._manifest("build a portfolio", "src/main.tsx")
    assert '"vite"' in manifest and '"react"' in manifest
