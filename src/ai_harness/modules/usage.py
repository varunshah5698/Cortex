"""Usage accounting: tokens, latency, cost and whether the numbers were measured.

Providers do not all report token counts. When a provider omits them the harness
estimates, and says so (`estimated: true`) instead of presenting an approximation as a
measurement — the difference matters when the number is used to stop a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token, never zero."""
    return max(1, len(text) // 4)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ms: int = 0
    calls: int = 0
    thinking_chars: int = 0
    cost_usd: float = 0.0
    cached: int = 0
    estimated: bool = False

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.ms += other.ms
        self.calls += other.calls
        self.thinking_chars += other.thinking_chars
        self.cost_usd = round(self.cost_usd + other.cost_usd, 6)
        self.cached += other.cached
        self.estimated = self.estimated or other.estimated

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "ms": self.ms,
            "calls": self.calls,
            "thinking_chars": self.thinking_chars,
            "cost_usd": self.cost_usd,
            "cached": self.cached,
            "estimated": self.estimated,
        }


@dataclass
class StreamStats:
    usage: Usage = field(default_factory=Usage)
    content: list[str] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.content)
