"""Allow / ask / deny policy for the actions the harness can take.

A pattern rule is a filter, not a sandbox: it stops the mistakes a run actually makes,
it does not contain a hostile model. Two things follow from that, and both are enforced
here rather than described in a prompt:

* A command is matched against an ordered rule list — deny first, then ask, then allow —
  and anything unmatched is denied. A new capability is a rule, not an assumption.
* A process the harness starts for a model-authored project never inherits credentials.
  `safe_env` strips provider keys, tokens and the harness's own configuration, so
  installed code cannot read the owner's keys out of the environment.

`ASK` is a human checkpoint: `modules/runner.py` refuses to install or build until the
request carries an explicit approval, which is why a run cannot execute a model's
`package.json` install scripts on its own.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

# Credentials must never reach a child process started for a model-authored project.
_SECRET_ENV = re.compile(r"(API_?KEY|_KEY$|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
# Nor does installed code need to know anything about the harness itself.
_HARNESS_ENV = re.compile(r"^HARNESS_", re.I)

# Files the harness refuses to read or serve: credentials and its own trust store.
DENY_PATHS = (
    ".env",
    ".env.*",
    ".harness-token",
    ".harness-roster.json",
    ".harness-keys.json",
    ".harness-auth.db",
    "secrets/*",
    "*.pem",
    "*.key",
    "*id_rsa*",
)


@dataclass(frozen=True)
class Rule:
    pattern: str
    effect: str
    reason: str


# Harness-owned binaries are allowed even under a generic interpreter rule: `python *` is
# `ask` because it usually means the model's code, but `python static_server.py` is a file
# this repository wrote and the runner starts it for every static preview. Specific wins
# over generic here, before the generic rules are consulted.
HARNESS_OWNED: tuple[Rule, ...] = (
    Rule("*static_server.py*", ALLOW, "the harness's own static file server; reads only"),
)

# Ordered: deny beats ask beats allow, and an unmatched command is denied.
COMMAND_RULES: tuple[Rule, ...] = (
    # ---- deny: never, not even with approval ----
    Rule("*rm -rf*", DENY, "recursive delete outside the run's own tree"),
    Rule("*rm -fr*", DENY, "recursive delete outside the run's own tree"),
    Rule("*sudo *", DENY, "privilege escalation"),
    Rule("*chmod 777*", DENY, "world-writable permissions"),
    Rule("*git push*", DENY, "writing to a remote is the owner's decision"),
    Rule("*npm publish*", DENY, "publishing is the owner's decision"),
    Rule("*pip install git+*", DENY, "unpinned code from a git remote"),
    Rule("*curl *", DENY, "network egress the harness did not authorise"),
    Rule("*wget *", DENY, "network egress the harness did not authorise"),
    # ---- ask: needs an explicit approval from the person at the keyboard ----
    Rule("*npm install*", ASK, "runs dependency install scripts written by the model"),
    Rule("*npm ci*", ASK, "runs dependency install scripts written by the model"),
    Rule("*npm run build*", ASK, "executes build scripts written by the model"),
    Rule("*npm run dev*", ASK, "executes a dev server written by the model"),
    Rule("*npx *", ASK, "fetches and executes a package"),
    Rule("*pip install*", ASK, "installs packages the model declared"),
    Rule("*uvicorn *", ASK, "executes the model's Python application"),
    Rule("*node *", ASK, "executes JavaScript the model wrote"),
    Rule("*go build*", ASK, "compiles and links code the model wrote"),
    Rule("*cargo build*", ASK, "compiles and links code the model wrote"),
    Rule("*make*", ASK, "executes a Makefile the model wrote"),
    Rule("python *", ASK, "executes Python the model wrote"),
    Rule("python3 *", ASK, "executes Python the model wrote"),
    # ---- allow: harness-owned and read-only ----
    Rule("ls*", ALLOW, "read-only inspection"),
    Rule("cat *", ALLOW, "read-only inspection"),
    Rule("wc *", ALLOW, "read-only inspection"),
    Rule("head *", ALLOW, "read-only inspection"),
    Rule("tail *", ALLOW, "read-only inspection"),
    Rule("tree*", ALLOW, "read-only inspection"),
    Rule("grep *", ALLOW, "read-only inspection"),
)


def command_line(argv: list[str]) -> str:
    """What the rules match: the program's name plus its arguments.

    Matching on the basename keeps a rule like `python *` working for an interpreter
    invoked by absolute path, which is how the runner starts every process.
    """
    if not argv:
        return ""
    return " ".join([Path(argv[0]).name, *argv[1:]]).strip()


def denied_path(argv: list[str]) -> str | None:
    """The first argument naming a credential or trust-store path, if any."""
    for token in argv:
        candidate = token.split("=")[-1].strip("'\"")
        name = Path(candidate).name
        if any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(candidate, pattern) for pattern in DENY_PATHS):
            return candidate
    return None


def decision(argv: list[str]) -> tuple[str, str]:
    """`(effect, reason)` for a command. Unmatched commands are denied by default."""
    secret = denied_path(argv)
    if secret:
        return DENY, f"{secret} is a credential or trust-store path"
    line = command_line(argv)
    for rule in HARNESS_OWNED:
        if fnmatch.fnmatch(line, rule.pattern):
            return ALLOW, rule.reason
    for effect in (DENY, ASK):
        for rule in COMMAND_RULES:
            if rule.effect == effect and fnmatch.fnmatch(line, rule.pattern):
                return effect, rule.reason
    for rule in COMMAND_RULES:
        if rule.effect == ALLOW and fnmatch.fnmatch(line, rule.pattern):
            return ALLOW, rule.reason
    return DENY, "no rule allows this command; add one deliberately"


def safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a child process gets: no credentials, no harness settings."""
    env = {
        key: value for key, value in os.environ.items() if not _SECRET_ENV.search(key) and not _HARNESS_ENV.match(key)
    }
    env.update(extra or {})
    return env


def table() -> list[dict]:
    """The policy as data, for `/api/health` and for docs/tool-inventory.md."""
    return [
        {"pattern": rule.pattern, "effect": rule.effect, "reason": rule.reason}
        for rule in (*HARNESS_OWNED, *COMMAND_RULES)
    ]
