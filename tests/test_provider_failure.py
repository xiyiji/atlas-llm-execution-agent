"""A live-model failure must fail the task loudly and say what to fix — never ship demo output as a result."""

import asyncio

import httpx

from app import config, llm
from app.models import TaskStatus
from app.orchestrator import Orchestrator
from app.storage import STORE


def _ollama_404(monkeypatch):
    monkeypatch.setattr(llm, "_provider", "ollama")
    monkeypatch.setattr(config, "LLM_FALLBACK_TO_DEMO", False)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model 'llama3.2' not found, try pulling it first"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_provider_error_carries_an_actionable_hint(monkeypatch):
    _ollama_404(monkeypatch)
    try:
        asyncio.run(llm.complete("sys", "prompt"))
    except llm.ProviderError as exc:
        assert exc.provider == "ollama"
        assert "HTTP 404" in exc.detail and "not found" in exc.detail
        assert "ollama pull llama3.2" in exc.hint
        assert "404" in str(exc) and "ollama pull" in str(exc)
    else:
        raise AssertionError("expected ProviderError")


def test_task_fails_visibly_instead_of_returning_demo_output(monkeypatch):
    _ollama_404(monkeypatch)

    async def scenario():
        orchestrator = Orchestrator()
        task = orchestrator.create_task("Research the latest agent frameworks", False, "provider")
        for _ in range(400):
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DENIED}:
                break
            await asyncio.sleep(0.01)
        assert task.status == TaskStatus.FAILED
        assert task.result == ""  # no made-up report
        assert "ollama" in task.error and "ollama pull" in task.error
        types = [row["type"] for row in STORE.events_after(task.id, "provider")]
        assert "task.failed" in types and types[-1] == "stream.end"
        assert "task.completed" not in types

    asyncio.run(scenario())


def test_provider_error_is_not_retried_as_a_flaky_step(monkeypatch):
    """Planning uses demo output here; the provider dies on the first real step. It must not burn retries."""
    monkeypatch.setattr(config, "LLM_FALLBACK_TO_DEMO", False)
    calls = 0
    original = llm.complete

    async def planner_ok_then_provider_down(system, prompt, max_tokens=1200, **kwargs):
        nonlocal calls
        if "ATLAS_JSON_SAFETY" in system or "ATLAS_JSON_PLANNER" in system:
            return await original(system, prompt, max_tokens, **kwargs)
        calls += 1
        raise llm.ProviderError("groq", "HTTP 401 invalid api key", "The groq API key was rejected. Check the key in .env.")

    monkeypatch.setattr(llm, "complete", planner_ok_then_provider_down)

    async def scenario():
        orchestrator = Orchestrator()
        task = orchestrator.create_task("Prepare a concise project summary", False, "provider")
        for _ in range(400):
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                break
            await asyncio.sleep(0.01)
        assert task.status == TaskStatus.FAILED
        assert calls == 1
        assert task.steps[0].attempts == 1
        events = STORE.events_after(task.id, "provider")
        provider_events = [row for row in events if row["type"] == "provider.error"]
        assert provider_events and provider_events[0]["data"]["hint"].startswith("The groq API key")

    asyncio.run(scenario())


def test_health_reports_provider_problem(monkeypatch):
    monkeypatch.setattr(llm, "_provider", "ollama")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    status = asyncio.run(llm.probe())
    assert status["ready"] is False
    assert "not found" in status["problem"] and "qwen2.5:7b" in status["problem"]
    assert "ollama pull" in status["hint"]


def test_optional_fallback_keeps_old_behaviour(monkeypatch):
    _ollama_404(monkeypatch)
    monkeypatch.setattr(config, "LLM_FALLBACK_TO_DEMO", True)
    text = asyncio.run(llm.complete("sys", "prompt"))
    assert text and llm.last_error().startswith("ollama: HTTP 404")
