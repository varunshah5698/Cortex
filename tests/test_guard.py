"""The answer guard: a trailing planning/reflection section must never survive.

Prompt wording asks a model not to restate its reasoning; this is the deterministic
backstop that removes the section from the answer no matter what the model does. The
canned replies in `fixtures/reflection_leaks.py` are the real shapes seen in the UI.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from fixtures.reflection_leaks import CLEAN, LEAKS  # noqa: E402

from ai_harness.modules.guard import ReflectionGuard, strip_reflection  # noqa: E402


def test_trailing_reflection_is_removed_from_every_fixture(tmp_path: Path) -> None:
    for case in LEAKS:
        cleaned = strip_reflection(case["answer"])
        assert cleaned.stripped, case["name"]
        assert cleaned.text == case["expected"], (case["name"], cleaned.text)
        assert cleaned.heading, case["name"]
        assert cleaned.removed.strip(), case["name"]


def test_clean_answers_are_left_alone(tmp_path: Path) -> None:
    for answer in CLEAN:
        cleaned = strip_reflection(answer)
        assert not cleaned.stripped, answer
        assert cleaned.text == answer.strip(), cleaned.text


def test_only_the_trailing_section_goes(tmp_path: Path) -> None:
    """A section heading mid-answer is content: the rule cuts at the first one only."""
    answer = "Intro.\n\n## Planning Reflection\nreasoning here\n"
    cleaned = strip_reflection(answer)
    assert cleaned.text == "Intro.", cleaned.text
    # the report says which heading was recognised, for the event log
    assert cleaned.heading.lower() == "planning reflection", cleaned.heading


def test_an_answer_that_is_only_reflection_is_not_invented(tmp_path: Path) -> None:
    """With nothing before the heading there is no answer to protect, so nothing is cut."""
    cleaned = strip_reflection("Planning Reflection: I have no answer yet.")
    assert not cleaned.stripped
    assert cleaned.text == "Planning Reflection: I have no answer yet."


def test_the_guard_holds_back_a_heading_split_across_tokens(tmp_path: Path) -> None:
    source = LEAKS[0]["answer"]
    guard = ReflectionGuard()
    emitted: list[str] = []
    # one character at a time: the worst case for a streaming filter
    for char in source:
        chunk = guard.feed(char)
        if chunk:
            emitted.append(chunk)
    emitted.append(guard.finish())
    shown = "".join(emitted)

    assert shown == LEAKS[0]["expected"], shown
    assert "Planning Reflection" not in shown
    assert guard.stripped
    # a half-streamed heading is not reported: the name is complete
    assert guard.heading == "Planning Reflection (Atlas)", guard.heading


def test_the_guard_passes_clean_text_through_with_bounded_lag(tmp_path: Path) -> None:
    answer = CLEAN[0]
    guard = ReflectionGuard()
    shown = ""
    for char in answer:
        shown += guard.feed(char)
    shown += guard.finish()
    assert shown == answer
    assert not guard.stripped and guard.heading is None


if __name__ == "__main__":
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    fn(Path(tmp).resolve())
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    print(f"FAIL {name}: {exc}")
                else:
                    print(f"ok   {name}")
    raise SystemExit(1 if failures else 0)
