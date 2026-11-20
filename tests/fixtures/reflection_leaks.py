"""Canned answers: model replies that leaked their reasoning, and clean ones.

Each leak names the shape it reproduces and the exact answer that should remain.
"""

from __future__ import annotations

ANSWER = "A closure captures the variables of its enclosing scope."

LEAKS: list[dict[str, str]] = [
    {
        "name": "the reported leak: rule, heading, bold reasoning summary",
        "answer": (
            ANSWER + "\n\n***\n\nPlanning Reflection (Atlas)\n\n"
            "**Reasoning Summary:** I restated the question, considered three angles, "
            "and chose the definition that fits the examples.\n"
        ),
        "expected": ANSWER,
    },
    {
        "name": "markdown heading reasoning summary",
        "answer": ANSWER + "\n\n### Reasoning Summary\nI compared options before answering.\n",
        "expected": ANSWER,
    },
    {
        "name": "decision rationale after a horizontal rule",
        "answer": ANSWER + "\n\n---\nDecision Rationale:\nThe second phrasing is clearer.\n",
        "expected": ANSWER,
    },
    {
        "name": "self-check appended as a label",
        "answer": ANSWER + "\n\nSelf-check: yes, that covers the question.\n",
        "expected": ANSWER,
    },
    {
        "name": "planning notes heading",
        "answer": ANSWER + "\n\n## Planning Notes\n- restate\n- answer\n",
        "expected": ANSWER,
    },
    {
        "name": "assumptions made, underscored heading",
        "answer": ANSWER + "\n\n**Assumptions made:** that you meant lexical scope.\n",
        "expected": ANSWER,
    },
    {
        "name": "uncertainties and trade-offs",
        "answer": ANSWER + "\n\nUncertainties and trade-offs: none worth naming.\n",
        "expected": ANSWER,
    },
    {
        "name": "two trailing sections: everything after the first goes",
        "answer": (ANSWER + "\n\n### Planning Reflection\nfirst pass\n\n### Self-check\nlooks fine\n"),
        "expected": ANSWER,
    },
]

CLEAN: list[str] = [
    ANSWER,
    # a heading mid-answer is content, not self-talk
    "## How to close a loop\n\nUse `for` when you need the index.\n\n## When not to\n\nUse `each` instead.",
    # a bare "Notes"/"Assumptions" heading is a real section, so it stays
    ANSWER + "\n\n## Notes\nThe spec is unchanged.\n",
    ANSWER + "\n\nAssumptions: none.\n",
    # mentions inside a sentence never match: the rule is anchored to a line
    ANSWER + "\nI did a self-check of the numbers before sending this.",
    # fenced code is never inspected
    ANSWER + "\n\n```python\n# Planning notes\n# self-check: not a heading\nprint(1)\n```\n",
]
