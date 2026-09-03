"""DuckDuckGo retrieval with a strict no-network demo path."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .. import config, llm

_DEMO_RESULTS = [
    {
        "title": "Reference overview — official documentation",
        "url": "https://docs.python.org/3/",
        "snippet": "A deterministic demo source representing primary technical documentation.",
    },
    {
        "title": "Research reference — arXiv",
        "url": "https://arxiv.org/",
        "snippet": "A deterministic demo source representing current research literature.",
    },
    {
        "title": "Standards reference — NIST",
        "url": "https://www.nist.gov/artificial-intelligence",
        "snippet": "A deterministic demo source representing standards and risk guidance.",
    },
]


def _plain(markup: str) -> str:
    text = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", " ", markup, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _unwrap_ddg(url: str) -> str:
    parsed = urlparse(html.unescape(url))
    if "duckduckgo.com" in parsed.netloc and "uddg" in parse_qs(parsed.query):
        return unquote(parse_qs(parsed.query)["uddg"][0])
    return url


async def search(query: str, max_results: int = 5) -> list[dict]:
    if llm.is_demo():
        return [{**item, "snippet": f"{item['snippet']} Query: {query[:120]}"} for item in _DEMO_RESULTS[:max_results]]
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AtlasMVP/0.1)"}
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
            response = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
            response.raise_for_status()
        results = []
        pattern = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
        snippets = re.findall(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', response.text, re.I | re.S)
        for index, (url, title) in enumerate(pattern.findall(response.text)[:max_results]):
            results.append({"title": _plain(title), "url": _unwrap_ddg(url), "snippet": _plain(snippets[index]) if index < len(snippets) else ""})
        return results
    except Exception as exc:
        return [{"title": "Search unavailable", "url": "", "snippet": f"{type(exc).__name__}: {exc}"}]


async def fetch_page(url: str, max_chars: int = 3500) -> str:
    if llm.is_demo():
        return "Demo mode: external page fetching is disabled; the source metadata above is simulated."
    if not await _safe_public_url(url):
        return "Page unavailable: invalid URL"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AtlasMVP/0.1)"}
        current = url
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers=headers) as client:
            for _ in range(4):
                if not await _safe_public_url(current):
                    return "Page unavailable: URL resolves to a non-public network"
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        current = str(httpx.URL(current).join(response.headers.get("location", "")))
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(kind in content_type for kind in ("text/", "application/xhtml+xml")):
                        return "Page unavailable: unsupported content type"
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > config.WEB_MAX_RESPONSE_BYTES:
                            return "Page unavailable: response exceeded size limit"
                    return _plain(chunks.decode(response.encoding or "utf-8", errors="replace"))[:max_chars]
        return "Page unavailable: too many redirects"
    except Exception as exc:
        return f"Page fetch failed: {type(exc).__name__}: {exc}"


async def _safe_public_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.port not in {None, 80, 443}:
        return False
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError):
        return False
    return all(ipaddress.ip_address(address[4][0]).is_global for address in addresses)
