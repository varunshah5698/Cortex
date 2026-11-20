"""Shared models: a specialist (one provider+model+role) and a work unit."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}
# Public providers that answer without an account. A specialist pointed here is runnable
# from a cloud deployment where the visitor's own loopback Ollama is not reachable.
KEYLESS_HOSTS = {"text.pollinations.ai"}


@dataclass
class Specialist:
    id: str
    name: str
    role: str = "general"
    kind: str = "openai"
    baseUrl: str = "https://api.openai.com/v1"
    apiKey: str = ""
    model: str = ""

    @property
    def is_local(self) -> bool:
        try:
            return urlparse(self.baseUrl).hostname in LOCAL_HOSTS
        except ValueError:
            return False

    @property
    def ready(self) -> bool:
        try:
            host = urlparse(self.baseUrl).hostname
        except ValueError:
            host = None
        return bool(self.model and self.baseUrl and (self.apiKey or self.is_local or host in KEYLESS_HOSTS))

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "kind": self.kind,
            "model": self.model,
            "local": self.is_local,
        }


@dataclass
class WorkUnit:
    id: str
    title: str
    role: str
    file: str
    objective: str
    agent: str = ""
    status: str = "pending"
    lines: int = 0
    findings: list[str] = field(default_factory=list)


@dataclass
class Team:
    specialists: list[Specialist]

    def by_id(self, agent_id: str) -> Specialist | None:
        return next((s for s in self.specialists if s.id == agent_id), None)

    def role_match(self, role: str) -> list[Specialist]:
        return [s for s in self.specialists if s.role == role]
