"""Tier 3: ask the running service whether it actually serves something.

The first two tiers read files: structural contradictions (`nodes/verify.py`) and the
artifact's own compiler (`modules/toolchain.py`). Neither can see the failure that matters
most to the person looking at the preview — the page loads and the button does nothing,
the route 500s, the build's `/assets/index-abc.js` 404s.

This probes the live service the runner already started: fetch the page, require a real
response, and follow the assets the page itself references. No browser, no headless
dependency — the runner has a URL, so the check is one HTTP client and a set of facts.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx

REF = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)
HTML_TYPE = "text/html"
MIN_BYTES = 120
ASSETS = (".js", ".mjs", ".css")
EXTERNAL = ("http://", "https://", "//", "data:", "mailto:", "#", "javascript:")


def _issues(url: str, code: str, detail: str) -> dict:
    return {"file": url, "level": "error", "code": code, "detail": detail}


async def check(url: str | None, limit: int = 6) -> dict:
    """Probe `url` and the assets its HTML names. Returns a verify-shaped report."""
    if not url:
        return {
            "ok": False,
            "stage": "preview",
            "errors": 1,
            "warnings": 0,
            "checked": 0,
            "issues": [_issues("preview", "not_serving", "the run has no ready service to check")],
        }
    issues: list[dict] = []
    checked = 0
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url)
            checked += 1
            body = response.text if HTML_TYPE in response.headers.get("content-type", "") else ""
            if response.status_code >= 400:
                issues.append(_issues(url, "http_error", f"answered {response.status_code}"))
            elif not body and len(response.content) < MIN_BYTES:
                issues.append(
                    _issues(url, "empty_page", f"answered {response.status_code} with {len(response.content)} bytes")
                )
            elif body and "<html" not in body.lower() and "<body" not in body.lower():
                issues.append(_issues(url, "not_html", "answered but the body is not an HTML document"))

            base = str(response.url)
            for ref in REF.findall(body)[:12]:
                target = ref.strip()
                if not target or target.lower().startswith(EXTERNAL):
                    continue
                parsed = urlparse(target)
                if not parsed.path.lower().endswith(ASSETS):
                    continue
                absolute = urljoin(base, target)
                if urlparse(absolute).netloc != urlparse(base).netloc:
                    continue  # never probe another host on the page's say-so
                checked += 1
                asset = await client.get(absolute)
                if asset.status_code >= 400:
                    issues.append(
                        _issues(
                            absolute, "missing_asset", f"the page loads it but the server answered {asset.status_code}"
                        )
                    )
                if len(issues) >= limit:
                    break
    except httpx.HTTPError as exc:
        issues.append(_issues(url, "unreachable", f"{type(exc).__name__}: {exc}"[:200]))

    return {
        "ok": not issues,
        "stage": "preview",
        "errors": len(issues),
        "warnings": 0,
        "checked": checked,
        "issues": issues[:limit],
    }
