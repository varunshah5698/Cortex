"""Network guard: a provider URL must not become an SSRF primitive.

The harness accepts a base URL from whoever is talking to it, and then makes requests
with that person's key attached. Left unchecked, that is a way to make the harness
probe the local network, a cloud metadata endpoint, or a link-local service — with the
harness's process as the vantage point.

Rules:

* http/https only, no credentials embedded in the URL;
* link-local (169.254.0.0/16 and friends), multicast, unspecified and reserved
  addresses are always refused — that includes the cloud metadata endpoint;
* loopback and RFC1918 addresses are allowed only when local providers are enabled
  (Ollama is a supported default) or the host is named in HARNESS_ALLOWED_PROVIDER_HOSTS;
* every address the hostname resolves to is checked, so a DNS entry pointing at
  169.254.169.254 is refused too.

Resolution is cached briefly and run off the event loop; a provider request pays one
lookup per host per minute, not one per token.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit

from ..config.settings import ALLOW_LOCAL_PROVIDERS, ALLOWED_PROVIDER_HOSTS

CACHE_TTL = 60.0
RESOLVE_TIMEOUT = 5.0

_ALWAYS_BLOCKED = [
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, incl. cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
]

_cache: dict[str, tuple[float, list[str]]] = {}


class UnsafeEndpoint(ValueError):
    """Raised when a provider URL is not somewhere the harness may send a request."""


def _addresses(host: str, port: int) -> list[str]:
    cached = _cache.get(host)
    now = time.monotonic()
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    found = sorted({str(info[4][0]) for info in infos})
    _cache[host] = (now, found)
    return found


def _blocked(address: str, *, allow_local: bool) -> str:
    """Return a reason string when `address` may not be used."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "not an IP address"
    for network in _ALWAYS_BLOCKED:
        if ip.version == network.version and ip in network:
            return "link-local, multicast or reserved address"
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return "link-local, multicast or reserved address"
    if (ip.is_loopback or ip.is_private) and not allow_local:
        return "private or loopback address (set HARNESS_ALLOW_LOCAL_PROVIDERS=1 to allow)"
    return ""


def check_url_syntax(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) for a provider base URL, or UnsafeEndpoint."""
    raw = (url or "").strip()
    if not raw:
        raise UnsafeEndpoint("no base URL")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeEndpoint(f"scheme {parts.scheme or '(none)'!r} is not http or https")
    if parts.username or parts.password:
        raise UnsafeEndpoint("credentials in the URL are not allowed")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise UnsafeEndpoint("no host in the base URL")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeEndpoint(f"invalid port: {exc}") from exc
    return parts.scheme.lower(), host, port


async def ensure_allowed(url: str, *, allow_local: bool | None = None) -> str:
    """Validate a provider base URL; return it without a trailing slash."""
    scheme, host, port = check_url_syntax(url)
    local_ok = ALLOW_LOCAL_PROVIDERS if allow_local is None else allow_local
    if host in ALLOWED_PROVIDER_HOSTS:
        local_ok = True
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _addresses, host, port),
            timeout=RESOLVE_TIMEOUT,
        )
    except (OSError, socket.gaierror) as exc:
        raise UnsafeEndpoint(f"cannot resolve {host}: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise UnsafeEndpoint(f"resolving {host} timed out") from exc
    if not addresses:
        raise UnsafeEndpoint(f"{host} resolved to no addresses")
    for address in addresses:
        reason = _blocked(address, allow_local=local_ok)
        if reason:
            raise UnsafeEndpoint(f"{host} resolves to {address}: {reason}")
    return raw_base(url)


def raw_base(url: str) -> str:
    """The URL without a trailing slash, query string preserved."""
    return (url or "").strip().rstrip("/")


def is_local_url(url: str) -> bool:
    """Cheap, no-DNS check used for pricing and UI hints."""
    try:
        _, host, _ = check_url_syntax(url)
    except UnsafeEndpoint:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private
