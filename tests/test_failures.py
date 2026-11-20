"""The failure log is a table, not an append-only tail.

Rows used to land under "Still open" after the table had ended; a reader (and the audit
script) then misses them. `record` inserts into the table it finds.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import failures  # noqa: E402


def test_a_recorded_failure_lands_inside_the_table(monkeypatch, tmp_path):
    log = tmp_path / "failures.md"
    log.write_text(failures.HEADER + "\n## Still open\n\n- a known gap\n", encoding="utf-8")
    monkeypatch.setattr(failures, "LOG", log)

    assert failures.record(types.SimpleNamespace(run_id="r1", files={}), None, "provider error")

    text = log.read_text(encoding="utf-8")
    assert text.count("| date | run |") == 1
    table, prose = text.split("## Still open", 1)
    assert "`r1`" in table
    assert "`r1`" not in prose
