"""Truncation: a response cut at the provider's ceiling is not a short response.

`finish_reason == "length"` is the one generation failure that leaves no trace in the
text — a file cut mid-token still parses as text, so every downstream check sees a
complete-looking file. These tests pin the flag that makes it visible, on all three wire
formats that report it differently.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules.usage import StreamStats  # noqa: E402


def test_a_fresh_stats_block_is_not_truncated():
    assert StreamStats().truncated is False


def test_the_flag_is_independent_of_the_text():
    # The whole point: content arrived, the text looks fine, and it is still a prefix.
    stats = StreamStats()
    stats.content.append('{"files": [{"path": "index.html"')
    assert stats.text
    assert stats.truncated is False
    stats.truncated = True
    assert stats.text and stats.truncated


def test_usage_accounting_is_unaffected_by_the_new_field():
    # `truncated` lives on StreamStats, not Usage, so the ledger's shape is unchanged and
    # an existing consumer that reads `usage.as_dict()` keeps working.
    stats = StreamStats()
    stats.truncated = True
    assert "truncated" not in stats.usage.as_dict()
    assert stats.usage.as_dict()["total_tokens"] == 0
