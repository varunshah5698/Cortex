"""The harness owns the Vite shell, including the preview's base path.

The preview serves a run under a per-run token prefix. Absolute `/assets/...` URLs then
404, and the build renders a blank page even though `vite build` succeeded.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.nodes import planning  # noqa: E402


def test_the_vite_shell_uses_relative_assets_and_a_real_entry():
    assert 'base: "./"' in planning._vite_config()
    assert "/src/main.tsx" in planning._index("src/main.tsx")
    manifest = planning._manifest("build a portfolio", "src/main.tsx")
    assert '"vite"' in manifest and '"react"' in manifest
