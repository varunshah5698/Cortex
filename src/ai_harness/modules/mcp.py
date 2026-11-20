"""A read-only MCP client, for connecting the harness to one real system.

The guide's Part C is narrow on purpose: connect exactly one system, give it the smallest
useful capability, and write its blast radius in a sentence before wiring it. A harness
that mounts ten servers has not made its team smarter; it has given ten remote callers a
say in the build.

So this client is deliberately partial:

* **stdio only** — a local server the owner configured; no remote endpoints, no OAuth;
* **read-only only** — a tool is callable when the server advertises
  ``annotations.readOnlyHint`` or its name starts with a read verb. Anything else is
  refused by name, not silently dropped from a list;
* **allow-listed per server** — `harness.mcp.json` names the tools, and an unlisted tool is
  refused even when the server offers it.

Nothing here is loaded at import: `servers()` returns `{}` when there is no config, so an
unconfigured harness behaves exactly as before. Set it up by writing:

    {"servers": {"repo": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                          "allow": ["read_file", "list_directory", "search_files"]}}}

and the research node can then call those three tools; anything else answers with the
refusal rather than a result.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.settings import ROOT
from . import permissions, tool_log

CONFIG = Path(os.getenv("HARNESS_MCP_CONFIG", ROOT / "harness.mcp.json"))
TIMEOUT = 20.0
READ_VERBS = ("read", "list", "get", "search", "find", "stat", "query", "fetch", "describe", "inspect")
MAX_OUTPUT = 4000


class MCPError(RuntimeError):
    """A server that is not configured, not answering, or refusing the call."""


@dataclass
class Server:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    cwd: str = ""
    # Starting an MCP server is `ask` under the command policy (`npx *`, `node *`). A
    # config file that says `"allow_exec": true` is the owner pre-approving that one
    # command; without it the session refuses instead of spawning behind their back.
    allow_exec: bool = False


def servers() -> dict[str, Server]:
    """The configured servers, or none. A malformed file is ignored, not fatal."""
    if not CONFIG.is_file():
        return {}
    try:
        body = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    found: dict[str, Server] = {}
    for name, item in (body.get("servers") or {}).items():
        if not isinstance(item, dict) or not item.get("command"):
            continue
        found[str(name)] = Server(
            name=str(name),
            command=str(item["command"]),
            args=[str(arg) for arg in item.get("args") or []],
            allow=[str(tool) for tool in item.get("allow") or []],
            cwd=str(item.get("cwd") or ""),
            allow_exec=bool(item.get("allow_exec")),
        )
    return found


def read_only(tool: dict) -> bool:
    """Whether a server's tool is safe to call without asking a human first."""
    annotations = tool.get("annotations")
    if isinstance(annotations, dict) and annotations.get("readOnlyHint") is not None:
        return bool(annotations["readOnlyHint"])
    name = str(tool.get("name") or "").lower()
    return any(name.startswith(verb) for verb in READ_VERBS)


def permitted(server: Server, tool: dict) -> tuple[bool, str]:
    name = str(tool.get("name") or "")
    if server.allow and name not in server.allow:
        return False, f"{name} is not in this server's allow list"
    if not read_only(tool):
        return False, f"{name} is not read-only; this harness will not write through MCP"
    return True, ""


def _text(result: dict) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts = [str(item.get("text") or "") for item in content if isinstance(item, dict)]
        joined = "\n".join(part for part in parts if part)
    else:
        joined = json.dumps(result)[:MAX_OUTPUT]
    return joined[:MAX_OUTPUT]


class Session:
    """One live stdio conversation with one server."""

    def __init__(self, server: Server) -> None:
        self.server = server
        self.process: asyncio.subprocess.Process | None = None
        self._id = 0

    async def __aenter__(self) -> Session:
        argv = [self.server.command, *self.server.args]
        effect, reason = permissions.decision(argv)
        if effect == permissions.DENY:
            raise MCPError(f"refused by policy: `{permissions.command_line(argv)}` — {reason}")
        if effect == permissions.ASK and not self.server.allow_exec:
            raise MCPError(
                f'`{permissions.command_line(argv)}` needs approval — set "allow_exec": true on '
                f"{self.server.name} in {CONFIG.name} to approve this server once"
            )
        try:
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.server.cwd or None,
                env=permissions.safe_env({"NO_COLOR": "1"}),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            raise MCPError(f"could not start {self.server.name}: {exc}") from exc
        await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cortex-harness", "version": "1.0"},
            },
        )
        await self.notify("notifications/initialized", {})
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.close()
        return False

    async def notify(self, method: str, params: dict) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, payload: dict) -> None:
        if self.process is None or self.process.stdin is None:
            raise MCPError(f"{self.server.name} is not running")
        self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict) -> dict:
        if self.process is None or self.process.stdout is None:
            raise MCPError(f"{self.server.name} is not running")
        self._id += 1
        wanted = self._id
        await self._write({"jsonrpc": "2.0", "id": wanted, "method": method, "params": params})
        try:
            while True:
                raw = await asyncio.wait_for(self.process.stdout.readline(), TIMEOUT)
                if not raw:
                    raise MCPError(f"{self.server.name} closed its output")
                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue  # servers log to stdout; a non-JSON line is not a reply
                if message.get("id") != wanted:
                    continue
                if "error" in message:
                    raise MCPError(f"{method}: {message['error']}")
                return message.get("result") or {}
        except asyncio.TimeoutError:
            raise MCPError(f"{self.server.name} did not answer {method} within {TIMEOUT:.0f}s") from None

    async def tools(self) -> list[dict]:
        result = await self.request("tools/list", {})
        items = result.get("tools")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), 3.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                process.kill()
            except ProcessLookupError:
                pass


async def inventory(server_name: str) -> list[dict]:
    """What one server offers, with the verdict this client would apply to each tool."""
    server = servers().get(server_name)
    if server is None:
        raise MCPError(f"no MCP server named {server_name!r} in {CONFIG.name}")
    async with Session(server) as session:
        rows = []
        for tool in await session.tools():
            ok, why = permitted(server, tool)
            rows.append(
                {
                    "name": tool.get("name"),
                    "description": str(tool.get("description") or "")[:200],
                    "callable": ok,
                    "why": why,
                }
            )
        return rows


async def call(server_name: str, tool: str, arguments: dict[str, Any] | None = None, log=tool_log.null) -> str:
    """Call one allow-listed, read-only tool and return its text output."""
    server = servers().get(server_name)
    if server is None:
        raise MCPError(f"no MCP server named {server_name!r} in {CONFIG.name}")
    async with log("mcp.call", {"server": server_name, "tool": tool}) as entry:
        async with Session(server) as session:
            offered = next((item for item in await session.tools() if item.get("name") == tool), None)
            if offered is None:
                raise MCPError(f"{server_name} offers no tool named {tool!r}")
            ok, why = permitted(server, offered)
            if not ok:
                entry.fail(why)
                raise MCPError(why)
            result = await session.request("tools/call", {"name": tool, "arguments": arguments or {}})
            text = _text(result)
            entry.set_result(f"{len(text)} chars" if text else "empty result")
            return text
