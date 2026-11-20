"""Opt-in: a real hosted model must answer our native tool-call shape.

Pollinations is keyless and hosted; it caps anonymous requests at one every ~45 seconds.
This test is therefore opt-in (`HARNESS_LIVE_FREE=1`) and is not part of `verify.sh`.
The request contains only a prompt and a tool schema, never repository code.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import llm  # noqa: E402
from ai_harness.modules.agent_loop import TOOL_SPECS  # noqa: E402


@pytest.mark.skipif(os.getenv("HARNESS_LIVE_FREE") != "1", reason="live hosted-model test is opt-in")
def test_a_hosted_model_returns_our_native_tool_call() -> None:
    spec = Specialist(
        id="hosted-free",
        name="Pollinations",
        role="general",
        kind="openai",
        baseUrl="https://text.pollinations.ai/openai/v1",
        model="openai",
    )

    async def run():
        return await llm.call_tools(
            spec,
            [
                {"role": "system", "content": "You are a coding agent. Use the read tool when asked."},
                {"role": "user", "content": "Call read for index.html. Do not answer in prose."},
            ],
            TOOL_SPECS,
            temperature=0,
        )

    call, text, usage = asyncio.run(run())
    assert call is not None, (text, usage)
    assert call["tool"] == "read"
    assert call["args"].get("path") == "index.html"
    assert usage.calls == 1
