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
LOGS_ROOT = ROOT / "logs"
DIST_DIR = ROOT / "dist"

HOST = os.getenv("HARNESS_HOST", "127.0.0.1")
PORT = _int("HARNESS_PORT", 8000)

# Reasoning and execution limits
MAX_UNITS = max(1, _int("HARNESS_MAX_UNITS", 4))
MAX_RESEARCH_QUERIES = _int("HARNESS_MAX_RESEARCH_QUERIES", 3)
MAX_RESEARCH_PAGES = _int("HARNESS_MAX_RESEARCH_PAGES", 2)
REASONING_WORDS = _int("HARNESS_REASONING_WORDS", 320)
# A second, adversarial read of the plan before code is written. It costs one call per
# file and catches broken wiring on paper; deep runs keep it on by default.
REASONING_CRITIC = _bool("HARNESS_REASONING_CRITIC", True)
HTTP_TIMEOUT_READ = _float("HARNESS_HTTP_TIMEOUT_READ", 300.0)
MAX_OUTPUT_TOKENS = _int("HARNESS_MAX_OUTPUT_TOKENS", 4096)
MAX_FILE_LINES = _int("HARNESS_MAX_FILE_LINES", 900)

# Wall-clock and concurrency control. A run is bounded in time as well as in money.
MAX_RUN_SECONDS = _float("HARNESS_MAX_SECONDS", 900.0)
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

# Memory hygiene: the event log is streamed to disk and only a window stays resident.
EVENT_MEMORY_LIMIT = max(200, _int("HARNESS_EVENT_MEMORY", 4000))
TRANSCRIPT_MAX_BYTES = max(64 * 1024, _int("HARNESS_TRANSCRIPT_BYTES", 8 * 1024 * 1024))

# Cost control: a run stops at whichever limit is reached first (0 disables a limit).
MAX_RUN_TOKENS = _int("HARNESS_MAX_TOKENS", 600000)
MAX_RUN_USD = _float("HARNESS_MAX_USD", 5.0)
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
RETRY_ATTEMPTS = _int("HARNESS_RETRY_ATTEMPTS", 3)
RETRY_BASE_DELAY = _float("HARNESS_RETRY_DELAY", 0.75)
RETRY_MAX_DELAY = _float("HARNESS_RETRY_MAX_DELAY", 30.0)

# Ask OpenAI-compatible providers for JSON mode on structured calls. Providers that
# reject the field are retried without it (see modules/adapters.py).
JSON_MODE = _bool("HARNESS_JSON_MODE", True)

# Provider rate limiting: requests per minute per (base URL, model). 0 disables it.
PROVIDER_RATE_LIMIT = _int("HARNESS_RATE_LIMIT_PER_MIN", 60)

# ---------- HTTP surface ----------

# A browser page on another origin must not be able to drive the harness (and spend
# the owner's keys). Only these origins may talk to /api, and only with the token.
DEFAULT_ORIGINS = "http://127.0.0.1:5178,http://localhost:5178"
ORIGINS = [
    origin.strip().rstrip("/") for origin in os.getenv("HARNESS_ORIGINS", DEFAULT_ORIGINS).split(",") if origin.strip()
]

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

TOKEN_FILE = Path(os.getenv("HARNESS_TOKEN_FILE", WORKSPACE_ROOT / ".harness-token"))
ROSTER_FILE = Path(os.getenv("HARNESS_ROSTER_FILE", WORKSPACE_ROOT / ".harness-roster.json"))
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
