"""Configuration for the AI harness: paths, limits and defaults.

Values come from the process environment. A `.env` file at the project root is read
first, with real environment variables always winning — the README tells people to
tune the harness there, so the file has to be load-bearing rather than decorative.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent


def load_env(path: Path) -> int:
    """Read a dotenv file into os.environ without overriding what is already set.

    Deliberately small (no dependency): KEY=VALUE lines, `#` comments, optional
    `export `, single or double quoted values. Returns the number of keys applied.
    """
    if not path.is_file():
        return 0
    applied = 0
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or entry.startswith("[") or "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key or not key.replace("_", "").isalnum() or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        os.environ[key] = value
        applied += 1
    return applied


ENV_FILE = Path(os.getenv("HARNESS_ENV_FILE", ROOT / ".env"))
ENV_KEYS_LOADED = load_env(ENV_FILE)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Fallback model: retries handle a flaky provider, but a hard outage on the only model
# ends the run. When set, the same agent retries once against this model.
FALLBACK_MODEL = os.getenv("HARNESS_FALLBACK_MODEL", "").strip()
FALLBACK_BASE_URL = os.getenv("HARNESS_FALLBACK_BASE_URL", "").strip()
FALLBACK_KIND = os.getenv("HARNESS_FALLBACK_KIND", "").strip()

WORKSPACE_ROOT = Path(os.getenv("HARNESS_WORKSPACE", ROOT / "workspace"))
RUNS_ROOT = WORKSPACE_ROOT / "runs"
# Accounts and sessions. On Render this sits beside the workspace; point it at a disk
# path to survive restarts and free-tier sleeps.
# Accounts, sessions and harness state live outside the generated workspace: a run can
# rewrite its workspace, but a login, roster or token should survive that.
AUTH_DB = Path(os.getenv("HARNESS_AUTH_DB", str(ROOT / ".harness-auth.db")))
# A free Render instance has no persistent disk. `/var/tmp` is wiped on restart, so the
# login page warns instead of pretending an account will still exist tomorrow.
AUTH_DURABLE = not str(AUTH_DB).startswith("/var/tmp")
LOGS_ROOT = ROOT / "logs"
DIST_DIR = ROOT / "dist"

HOST = os.getenv("HARNESS_HOST", "127.0.0.1")
PORT = _int("HARNESS_PORT", 8000)

# Reasoning and execution limits
# 0 means no cap: the planner names as many files as the work needs, and an agent may
# add more behind `<<<FILE path>>>`. The file count is not a design constraint.
MAX_UNITS = max(0, _int("HARNESS_MAX_UNITS", 0))
MAX_RESEARCH_QUERIES = _int("HARNESS_MAX_RESEARCH_QUERIES", 3)
MAX_RESEARCH_PAGES = _int("HARNESS_MAX_RESEARCH_PAGES", 2)
REASONING_WORDS = _int("HARNESS_REASONING_WORDS", 700)
# A small model can ignore the word budget and stream forever, producing no file while
# the room looks alive. The cap is a hard stop on the visible reasoning stream; the
# compose pass then gets its turn.
MAX_REASONING_CHARS = max(1000, _int("HARNESS_MAX_REASONING_CHARS", REASONING_WORDS * 6))
# The reviewer streams its reading of the tree, and that stream was unbounded: a local
# model that keeps thinking produced 7,000+ deltas and held one review call open for
# three quarters of an hour while the room looked alive. The reasoning pass already has
# a budget (`MAX_REASONING_CHARS`); the review pass gets its own, because it is a
# different job and a shorter one.
REVIEW_THINK_CHARS = max(600, _int("HARNESS_REVIEW_THINK_CHARS", 2400))
# A single provider call may not outlive this, whatever the provider is doing. The run
# deadline is checked before a call; this is checked *during* one, so a stream that
# stalls or a model that will not stop is cut instead of holding the run open until the
# wall-clock limit two hours later.
STREAM_SECONDS = max(0.0, _float("HARNESS_STREAM_SECONDS", 180.0))
# Reasoning deltas are emitted in batches rather than one event per token. A 4-character
# delta per event wrote 8,000 events and pushed 8,000 SSE messages for one review; the
# transcript is for reconstruction, not for replaying the tokenizer.
THINK_BATCH_CHARS = max(0, _int("HARNESS_THINK_BATCH_CHARS", 120))
THINK_BATCH_MS = max(0, _int("HARNESS_THINK_BATCH_MS", 120))
# A planner that has not produced a plan by now is not going to. The node falls back to
# the stack inferred from the request and starts the room writing, rather than leaving
# the UI at "planning" forever.
# 0 disables the planner deadline. The default is bounded because the alternative on a
# slow local model is a room stuck at "planning" with no file; the fallback scaffold is
# still runnable and the reviewers can improve it.
PLAN_SECONDS = max(0.0, _float("HARNESS_PLAN_SECONDS", 120.0))
# A second, adversarial read of the plan before code is written. It costs one call per
# file and catches broken wiring on paper; deep runs keep it on by default.
REASONING_CRITIC = _bool("HARNESS_REASONING_CRITIC", True)
HTTP_TIMEOUT_READ = _float("HARNESS_HTTP_TIMEOUT_READ", 300.0)
MAX_OUTPUT_TOKENS = _int("HARNESS_MAX_OUTPUT_TOKENS", 8192)
MAX_FILE_LINES = _int("HARNESS_MAX_FILE_LINES", 900)

# Wall-clock and concurrency control. A run is bounded in time as well as in money.
# Local models make one call at a time and a multi-file build is a dozen calls; an hour
# is the ceiling that still lets a local run finish. Hosted runs are bounded by tokens/$.
MAX_RUN_SECONDS = _float("HARNESS_MAX_SECONDS", 7200.0)
MAX_CONCURRENT_RUNS = max(1, _int("HARNESS_MAX_CONCURRENT_RUNS", 2))
MAX_QUEUED_RUNS = max(0, _int("HARNESS_MAX_QUEUED_RUNS", 8))
MAX_LIVE_RUNS = max(4, _int("HARNESS_MAX_LIVE_RUNS", 24))

# Disk quotas, enforced at the tool boundary rather than after the fact.
MAX_FILE_BYTES = max(1024, _int("HARNESS_MAX_FILE_BYTES", 512 * 1024))
MAX_RUN_BYTES = max(MAX_FILE_BYTES, _int("HARNESS_MAX_RUN_BYTES", 4 * 1024 * 1024))
MAX_REVISION_ROUNDS = max(0, _int("HARNESS_MAX_REVISION_ROUNDS", 2))

# A run may not finish on a failing check: after the review's own revision pass the
# orchestrator allows this many repair rounds before it refuses to emit `done`.
REPAIR_ROUNDS = max(0, _int("HARNESS_REPAIR_ROUNDS", 1))

# The repair agent loop: a specialist fixes a failing check with tools (read, edit,
# check) instead of rewriting the whole file blind. It is on by default because a
# targeted edit is cheaper and safer than a second full generation; the old rewrite is
# still the fallback when a model cannot produce a usable tool call.
AGENT_LOOP = _bool("HARNESS_AGENT_LOOP", True)
AGENT_STEPS = max(1, _int("HARNESS_AGENT_STEPS", 8))
# The prompt is rebuilt each turn from a flat state block, not from a growing chat, so
# this is the whole working set: ledger + active files + observations.
AGENT_CONTEXT_CHARS = max(4000, _int("HARNESS_AGENT_CONTEXT_CHARS", 12000))
AGENT_OBSERVATION_CHARS = max(500, _int("HARNESS_AGENT_OBSERVATION_CHARS", 4000))
AGENT_ACTIVE_FILES = max(1, _int("HARNESS_AGENT_ACTIVE_FILES", 2))
# OpenAI-compatible providers can be driven by native tool calls instead of the text
# protocol. The loop keeps the text parser as the fallback and disables this for the
# rest of the run when a provider answers 4xx to `tools`.
NATIVE_TOOLS = _bool("HARNESS_NATIVE_TOOLS", True)

# The shell tool is the workshop's `bash`, contained: one allow-listed command, no shell
# metacharacters, judged by `permissions.decision`, wrapped by `sandbox.prefix`, and off
# unless an operator turns it on. It also requires a real sandbox — where there is none it
# reports itself unavailable rather than running with the owner's access.
AGENT_SHELL = _bool("HARNESS_AGENT_SHELL", False)

# ---------- model roulette: vary the model behind a role, per call ----------
# Off by default: a run that silently changes models is a run whose cost and behaviour
# cannot be compared to the last one. On, it draws from the roster at every call, which
# is the research setting — alternating between two strong models beats either alone
# because they fail differently. `HARNESS_ROULETTE_MODELS` is a comma-separated list of
# model names (not specialist ids); a name with no configured key is ignored.
ROULETTE_ENABLED = _bool("HARNESS_ROULETTE", False)
ROULETTE_MODELS = [name.strip() for name in os.getenv("HARNESS_ROULETTE_MODELS", "").split(",") if name.strip()]
# A seed makes the draw sequence reproducible, so a surprising run can be replayed. Unset
# means the process RNG, which is what an experiment wants.
ROULETTE_SEED = _int("HARNESS_ROULETTE_SEED", 0) or None

# A subagent gets its own message list and a bounded number of steps. The bound is
# smaller than the repair loop's: a delegate that needs ten tool calls to answer one
# question is usually reading the wrong thing, and the parent is paying for every step.
SUBAGENT_STEPS = max(1, _int("HARNESS_SUBAGENT_STEPS", 6))

# The acceptance layer: the request's named capabilities become a checklist the harness
# checks the tree against. This is the only sensor that asks whether the build is the
# thing that was asked for, and it is what keeps a weak model from shipping a tidy page
# with none of the requested features. Off only for an operator who wants structure-only
# verification.
INTENT_CHECK = _bool("HARNESS_INTENT_CHECK", True)

# ---------- hooks: declarative extension points around every tool call ----------
# Rules live in `harness.hooks.json` and are matched against the tool name and an
# optional path glob. They are declarative on purpose: a hook that could run a shell
# command would be model-reachable code execution outside `modules/sandbox.py`. A
# missing or malformed file is inert, so an unconfigured harness is unchanged.
HOOKS_ENABLED = _bool("HARNESS_HOOKS", True)
HOOKS_FILE = Path(os.getenv("HARNESS_HOOKS_FILE", ROOT / "harness.hooks.json"))

# ---------- context compaction: make room instead of overflowing ----------
# A long repair loop or a resumed run can outgrow the window. Compaction is tiered and
# cheap-first: clip tool results, then drop the oldest turns, then summarise. The
# summary is a model call, so it is the last resort and it is bounded.
COMPACT_ENABLED = _bool("HARNESS_COMPACT", True)
# The share of the window a request may occupy before compaction runs.
COMPACT_AT = min(0.95, max(0.3, _float("HARNESS_COMPACT_AT", 0.75)))
# Tool results are the first thing to shrink: they are the bulk and the least precious.
COMPACT_TOOL_CHARS = max(200, _int("HARNESS_COMPACT_TOOL_CHARS", 1200))
# How many of the newest turns are never touched, whatever their size.
COMPACT_KEEP_TURNS = max(1, _int("HARNESS_COMPACT_KEEP_TURNS", 4))
# The summary call is capped so a compaction can never cost more than the work it saves.
COMPACT_SUMMARY_TOKENS = max(120, _int("HARNESS_COMPACT_SUMMARY_TOKENS", 400))
# The window compaction measures against. A roster row does not carry one — providers do
# not agree on a field for it, and a wrong guess is worse than a stated default — so this
# is the number the harness assumes, and an operator with a smaller model lowers it.
CONTEXT_WINDOW = max(2000, _int("HARNESS_CONTEXT_WINDOW", 128000))

# ---------- the goal gate: an independent check decides when a run may stop ----------
# `done` is already gated on the verifier. This is the second gate for a *goal*: the run
# states what finished looks like, and a separate evaluation says whether that is true.
# It exists because a model that is tired of a task reports success; the gate is the
# harness's answer, and it can send the run back for another pass.
GOAL_ENABLED = _bool("HARNESS_GOAL_GATE", True)
# How many times the gate may send a run back before it accepts the stop and records why.
GOAL_ROUNDS = max(0, _int("HARNESS_GOAL_ROUNDS", 1))
# A goal the harness can check itself is checked by the verifier, not by a model. Only a
# goal with no mechanical sensor reaches the model evaluator.
GOAL_EVALUATOR = _bool("HARNESS_GOAL_EVALUATOR", True)

# ---------- the clean-state gate: a run may not end leaving the next one a mess ----------
# Lecture 12's checklist, answered from disk instead of ticked by the model. An empty
# planned file and a feature passing without evidence are errors; a missing handoff is a
# warning. Off only for an operator who wants the old end-of-run behaviour.
CLEAN_STATE = _bool("HARNESS_CLEAN_STATE", True)

# ---------- the evaluator rubric: grade the session, not only the tree ----------
# Six categories, each scored 0-2, producing Accept / Revise / Block. It is a model call
# that did not write the work, so it is opt-in and bounded; the mechanical gates
# (verifier, goal, clean state) run first and can refuse a run without it.
RUBRIC_ENABLED = _bool("HARNESS_RUBRIC", False)

# Memory hygiene: the event log is streamed to disk and only a window stays resident.
EVENT_MEMORY_LIMIT = max(200, _int("HARNESS_EVENT_MEMORY", 4000))
TRANSCRIPT_MAX_BYTES = max(64 * 1024, _int("HARNESS_TRANSCRIPT_BYTES", 8 * 1024 * 1024))

# Cost control: a run stops at whichever limit is reached first (0 disables a limit).
MAX_RUN_TOKENS = _int("HARNESS_MAX_TOKENS", 1500000)
MAX_RUN_USD = _float("HARNESS_MAX_USD", 20.0)
MAX_RUN_CALLS = _int("HARNESS_MAX_CALLS", 0)

# Sampling temperature. Above 0 the provider samples, so the same prompt can produce
# different code; 0 is reproducible. Only temperature 0 can be cached.
TEMPERATURE = _float("HARNESS_TEMPERATURE", 0.35)

# Off by default, and even when enabled the cache only stores deterministic requests
# (`CACHE_ENABLED and TEMPERATURE == 0`): replaying a sampled generation would silently
# serve a different build than the model would write now. See modules/cache.py.
CACHE_ENABLED = _bool("HARNESS_CACHE", False)
CACHE_ENTRIES = _int("HARNESS_CACHE_ENTRIES", 256)

# Transient provider failures (429/5xx/timeouts) are retried; auth errors never are.
# Five attempts with 2s, 4s, 8s and 16s waits covers an account rate-limit window;
# `Retry-After` from the provider is honoured even when it is longer.
RETRY_ATTEMPTS = _int("HARNESS_RETRY_ATTEMPTS", 5)
RETRY_BASE_DELAY = _float("HARNESS_RETRY_DELAY", 2.0)
RETRY_MAX_DELAY = _float("HARNESS_RETRY_MAX_DELAY", 60.0)

# Ask OpenAI-compatible providers for JSON mode on structured calls. Providers that
# reject the field are retried without it (see modules/adapters.py).
JSON_MODE = _bool("HARNESS_JSON_MODE", True)

# Provider rate limiting: requests per minute per (base URL, model). 0 disables it.
PROVIDER_RATE_LIMIT = _int("HARNESS_RATE_LIMIT_PER_MIN", 60)

# In-flight requests per (base URL, model). Gateway accounts such as River publish no
# fixed allowance and answer 429 when concurrency is too high; 1 serializes each model.
# 0 leaves concurrency to the provider.
PROVIDER_CONCURRENCY = max(0, _int("HARNESS_PROVIDER_CONCURRENCY", 0))

# ---------- HTTP surface ----------

# A browser page on another origin must not be able to drive the harness (and spend
# the owner's keys). Only these origins may talk to /api, and only with the token.
DEFAULT_ORIGINS = "http://127.0.0.1:5178,http://localhost:5178"
ORIGINS = [
    origin.strip().rstrip("/") for origin in os.getenv("HARNESS_ORIGINS", DEFAULT_ORIGINS).split(",") if origin.strip()
]

# The public URL of a deployed instance. Render sets RENDER_EXTERNAL_URL; other hosts
# can set HARNESS_PUBLIC_ORIGIN. Same-origin requests from this host are the UI, not a
# foreign page, so the API guard treats it like loopback. Empty on a local checkout.
_public_url = os.getenv("HARNESS_PUBLIC_ORIGIN") or os.getenv("RENDER_EXTERNAL_URL") or ""
if not _public_url and os.getenv("RENDER_EXTERNAL_HOSTNAME"):
    _public_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', '').strip()}"
PUBLIC_ORIGIN = _public_url.strip().rstrip("/")
if PUBLIC_ORIGIN and PUBLIC_ORIGIN not in ORIGINS:
    ORIGINS.append(PUBLIC_ORIGIN)

# Local providers (Ollama, a local vLLM) are a supported feature, so loopback and
# RFC1918 hosts are allowed by default. Set HARNESS_ALLOW_LOCAL_PROVIDERS=0 on a shared
# machine. Link-local/metadata addresses are blocked either way (utils/netguard.py).
ALLOW_LOCAL_PROVIDERS = _bool("HARNESS_ALLOW_LOCAL_PROVIDERS", True)
ALLOWED_PROVIDER_HOSTS = {
    host.strip().lower() for host in os.getenv("HARNESS_ALLOWED_PROVIDER_HOSTS", "").split(",") if host.strip()
}

# Per-client HTTP rate limit (requests/minute) for the API itself. 0 disables it.
HTTP_RATE_LIMIT = _int("HARNESS_HTTP_RATE_LIMIT", 240)

# Artifacts (generated pages, served runs) live on their own origin, so a script a model
# wrote is never same-origin with /api. Loopback only; there is no token on this port
# because it serves nothing but the run's own files.
PREVIEW_HOST = os.getenv("HARNESS_PREVIEW_HOST", "127.0.0.1").strip() or "127.0.0.1"
PREVIEW_PORT = _int("HARNESS_PREVIEW_PORT", 8001)

# Tier 4: a real browser. The HTTP probe cannot see a button that does nothing or a
# console exception that blanks the page. Chrome is driven over CDP by
# scripts/browser_check.mjs, so this needs no npm dependency; set HARNESS_BROWSER_CHECK=0
# on a machine without Chrome. Interaction clicks up to two visible buttons, which is
# the check that catches "it renders and nothing works".
BROWSER_CHECK = _bool("HARNESS_BROWSER_CHECK", True)
BROWSER_INTERACT = _bool("HARNESS_BROWSER_INTERACT", True)
BROWSER_TIMEOUT = _float("HARNESS_BROWSER_TIMEOUT", 30.0)
CHROME_PATH = os.getenv("HARNESS_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").strip()
NODE_BIN = os.getenv("HARNESS_NODE", "node").strip() or "node"

# A public share is a per-run tunnel, not a second /api. The tunnel command is opt-in
# (the API requires approval), TTL-bounded, and only ever points at the one-run share
# server. `{url}` is replaced with that server's loopback URL.
TUNNEL_COMMAND = os.getenv("HARNESS_TUNNEL_COMMAND", "").strip()
TUNNEL_TIMEOUT = _float("HARNESS_TUNNEL_TIMEOUT", 30.0)
SHARE_TTL = _float("HARNESS_SHARE_TTL", 900.0)

# Per-run cloud execution. `HARNESS_RUNNER_MODE=cloud` sends the workspace to a separately
# deployed `ai_harness.cloud_runner` service, which installs, serves and proxies it. The
# token is server-to-server only; the browser receives the run's capability URL, never
# this token. The URL and token are absent locally, so local builds are unaffected.
RUNNER_MODE = os.getenv("HARNESS_RUNNER_MODE", "local").strip().lower()
_cloud_runner_url = os.getenv("HARNESS_CLOUD_RUNNER_URL", "").strip().rstrip("/")
# Render's `fromService` reference gives a hostname, not a URL. The runner is always
# HTTPS on Render; a local URL already carries its scheme.
if _cloud_runner_url and "://" not in _cloud_runner_url:
    _cloud_runner_url = f"https://{_cloud_runner_url}"
CLOUD_RUNNER_URL = _cloud_runner_url
CLOUD_RUNNER_TOKEN = os.getenv("HARNESS_CLOUD_RUNNER_TOKEN", "").strip()
CLOUD_RUNNER_TIMEOUT = max(10.0, _float("HARNESS_CLOUD_RUNNER_TIMEOUT", 120.0))

# Per-run Vercel deployments: `HARNESS_RUNNER_MODE=vercel` hosts static/Node runs on
# Vercel and removes them after the TTL. The CLI login on this machine is enough; a
# server-to-server token is only needed on a headless host.
VERCEL_BIN = os.getenv("HARNESS_VERCEL_BIN", "vercel").strip() or "vercel"
VERCEL_TOKEN = os.getenv("HARNESS_VERCEL_TOKEN", "").strip()
VERCEL_SCOPE = os.getenv("HARNESS_VERCEL_SCOPE", "").strip()
VERCEL_TIMEOUT = max(60.0, _float("HARNESS_VERCEL_TIMEOUT", 240.0))
VERCEL_TTL = _float("HARNESS_VERCEL_TTL", 3600.0)

TOKEN_FILE = Path(os.getenv("HARNESS_TOKEN_FILE", ROOT / ".harness-token"))
ROSTER_FILE = Path(os.getenv("HARNESS_ROSTER_FILE", ROOT / ".harness-roster.json"))
# Keys live outside the generated workspace and are stored per provider, so resetting
# the roster (or clearing the workspace) cannot lose a credential.
KEYS_FILE = Path(os.getenv("HARNESS_KEYS_FILE", ROOT / ".harness-keys.json"))

# Installs, builds and model-authored servers are an `ask` decision in
# modules/permissions.py: they do not run until a request carries an explicit approval.
# Turning this off removes the human checkpoint; it exists for unattended CI, not for
# normal use.
REQUIRE_APPROVAL = _bool("HARNESS_REQUIRE_APPROVAL", True)

TERMINAL_ENABLED = _bool("HARNESS_ALLOW_TERMINAL", False)
TERMINAL_TIMEOUT = _float("HARNESS_TERMINAL_TIMEOUT", 10.0)
TERMINAL_MAX_BYTES = _int("HARNESS_TERMINAL_MAX_BYTES", 64 * 1024)

USER_AGENT = "Mozilla/5.0 (compatible; CortexHarness/0.4; +https://localhost)"


def api_token() -> str:
    """The token every /api request must present.

    From HARNESS_TOKEN when set, otherwise generated once and kept in a 0600 file so
    the UI (and only the local user) can read it back across restarts.
    """
    explicit = os.getenv("HARNESS_TOKEN", "").strip()
    if explicit:
        return explicit
    try:
        if TOKEN_FILE.is_file():
            existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        token = secrets.token_urlsafe(24)
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token, encoding="utf-8")
        TOKEN_FILE.chmod(0o600)
        return token
    except OSError:
        # A read-only workspace must not take the whole API down.
        return secrets.token_urlsafe(24)


# ---------- provider keys ----------

# A key may live here instead of in the browser. `HARNESS_PROVIDER_KEY` is the fallback
# for a custom endpoint. A key saved through /api/roster wins over these.
PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "custom": "HARNESS_PROVIDER_KEY",
    "ollama": "",
}


def provider_key(kind: str) -> str:
    """The server-side key for a provider kind, or "" when none is configured."""
    name = PROVIDER_KEY_ENV.get(kind, "HARNESS_PROVIDER_KEY")
    return os.getenv(name, "").strip() if name else ""


for directory in (RUNS_ROOT, LOGS_ROOT):
    directory.mkdir(parents=True, exist_ok=True)
