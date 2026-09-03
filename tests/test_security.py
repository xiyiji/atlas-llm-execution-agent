"""Sandbox and retrieval hardening."""

import asyncio

import httpx
import pytest

from app.tools import code_exec, web


def test_sandbox_kills_runaway_code(monkeypatch):
    monkeypatch.setattr(code_exec, "CODE_TIMEOUT_SECONDS", 1)
    result = asyncio.run(code_exec.run_python("while True:\n    pass"))
    assert not result["ok"] and "Timed out" in result["stderr"]


@pytest.mark.parametrize(
    "snippet",
    [
        "import subprocess",
        "from os import system",
        "__import__('os')",
        "open('/etc/passwd').read()",
        "eval('1+1')",
        "import socket",
        "print(open('x'))",
    ],
)
def test_sandbox_policy_rejects_dangerous_constructs(snippet):
    result = asyncio.run(code_exec.run_python(snippet))
    assert not result["ok"] and "Rejected" in result["stderr"]


def test_sandbox_truncates_large_output():
    result = asyncio.run(code_exec.run_python("print('x' * 50000)"))
    assert result["ok"] and "truncated" in result["stdout"] and len(result["stdout"]) < 13_000


def test_fetch_page_blocks_redirect_into_private_network(monkeypatch):
    monkeypatch.setattr(web.llm, "is_demo", lambda: False)

    async def fake_safe(url: str) -> bool:
        return "example.com" in url

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://10.0.0.8/admin"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>internal</html>")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(web, "_safe_public_url", fake_safe)
    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))

    text = asyncio.run(web.fetch_page("https://example.com/start"))
    assert "non-public network" in text


def test_fetch_page_rejects_binary_and_oversized_responses(monkeypatch):
    monkeypatch.setattr(web.llm, "is_demo", lambda: False)
    monkeypatch.setattr(web.config, "WEB_MAX_RESPONSE_BYTES", 100)

    async def always_safe(url: str) -> bool:
        return True

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/binary":
            return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>" + "a" * 500 + "</p>")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(web, "_safe_public_url", always_safe)
    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))

    assert "unsupported content type" in asyncio.run(web.fetch_page("https://example.com/binary"))
    assert "size limit" in asyncio.run(web.fetch_page("https://example.com/big"))


def test_fetch_page_strips_scripts_and_tags(monkeypatch):
    monkeypatch.setattr(web.llm, "is_demo", lambda: False)

    async def always_safe(url: str) -> bool:
        return True

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><script>alert(1)</script><h1>Title</h1><p>Body &amp; text</p></html>")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(web, "_safe_public_url", always_safe)
    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    assert asyncio.run(web.fetch_page("https://example.com/")) == "Title Body & text"


def test_demo_mode_never_touches_the_network(monkeypatch):
    def explode(**kwargs):
        raise AssertionError("network client constructed in demo mode")

    monkeypatch.setattr(web.httpx, "AsyncClient", explode)
    assert asyncio.run(web.search("anything"))[0]["url"].startswith("https://")
    assert "Demo mode" in asyncio.run(web.fetch_page("https://example.com/"))
