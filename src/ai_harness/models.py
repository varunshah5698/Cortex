"""Shared models: a specialist (one provider+model+role) and a work unit."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


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
        return bool(self.model and self.baseUrl and (self.apiKey or self.is_local))

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
