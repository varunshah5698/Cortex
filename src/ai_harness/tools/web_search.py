"""Web tool: DuckDuckGo search plus readable page extraction. No API key required.

Both calls accept a `log` factory from `modules/tool_log.py`, so the run's transcript
records every query and every URL fetched — including the ones that returned nothing,
which is the failure a log otherwise hides.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from ..config.settings import MAX_RESEARCH_PAGES, USER_AGENT
from ..modules import tool_log
from ..modules.thrash import Thrash

TAG = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")
RESULT_LINK = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
LITE_LINK = re.compile(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)


def _text(fragment: str) -> str:
    return SPACE.sub(" ", html.unescape(TAG.sub("", fragment))).strip()


def _clean_url(url: str) -> str:
    if "duckduckgo.com/l/?uddg=" in url:
        query = parse_qs(urlparse(url).query)
        return unquote(query.get("uddg", [url])[0])
    return url


async def _search(query: str, limit: int) -> list[dict]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=headers)
            if response.status_code >= 400:
                response = await client.get(
                    f"https://lite.duckduckgo.com/lite/?q={httpx.QueryParams({'q': query})['q']}",
                    headers=headers,
                )
    except httpx.HTTPError:
        return []

    body = response.text
    links = RESULT_LINK.findall(body) or LITE_LINK.findall(body)
    snippets = [_text(s) for s in SNIPPET.findall(body)]
    results: list[dict] = []
    for index, (url, title) in enumerate(links[:limit]):
        results.append(
            {
                "title": _text(title)[:140],
                "url": _clean_url(html.unescape(url)),
                "snippet": snippets[index][:240] if index < len(snippets) else "",
            }
        )
    return results


async def search(query: str, limit: int = 4, log=tool_log.null) -> list[dict]:
    """Search the open web. Returns title/url/snippet; empty on failure."""
    try:
        async with log("web_search.search", {"query": query, "limit": limit}) as call:
            results = await _search(query, limit)
            if results:
                call.set_result(f"{len(results)} result(s) · first: {results[0]['title'][:80]}")
            else:
                call.set_result("0 results")
    except Thrash:
        # The same query is already refused and recorded; a loop must not end the run.
        return []
    return results


async def _fetch(url: str, limit: int) -> str:
    headers = {"User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return ""
    if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", ""):
        return ""
    body = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", response.text)
    return _text(body)[:limit]


async def fetch(url: str, limit: int = 2200, log=tool_log.null) -> str:
    """Fetch a page and return readable text."""
    try:
        async with log("web_search.fetch", {"url": url}) as call:
            text = await _fetch(url, limit)
            call.set_result(f"{len(text)} chars of readable text" if text else "nothing readable (empty or not HTML)")
    except Thrash:
        return ""
    return text


async def research(queries: list[str], log=tool_log.null) -> tuple[list[dict], list[str]]:
    """Run searches and read the top pages. Returns (sources, notes)."""
    sources: list[dict] = []
    notes: list[str] = []
    for query in queries[:3]:
        hits = await search(query, limit=3, log=log)
        for hit in hits:
            if hit["url"] not in {s["url"] for s in sources}:
                sources.append({**hit, "query": query})
        if hits:
            notes.append(f"Search '{query}': " + "; ".join(h["title"] for h in hits[:3]))

    for source in sources[:MAX_RESEARCH_PAGES]:
        text = await fetch(source["url"], log=log)
        if text:
            source["excerpt"] = text[:1200]
            notes.append(f"{source['url']}: {text[:420]}")
    return sources, notes
