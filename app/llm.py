"""Provider-neutral language-model access with deterministic demo fallback."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
from prometheus_client import Counter, Histogram

from . import config

LLM_CALLS = Counter("atlas_llm_calls_total", "Model calls by provider and outcome", ["provider", "outcome"])
LLM_LATENCY = Histogram("atlas_llm_call_duration_seconds", "Model call latency", ["provider"])


_provider: str | None = None
_last_error = ""


def detect_provider() -> str:
    global _provider
    if _provider is not None:
        return _provider
    if config.FORCE_DEMO:
        _provider = "demo"
    elif config.ANTHROPIC_API_KEY:
        _provider = "anthropic"
    elif config.CEREBRAS_API_KEY:
        _provider = "cerebras"
    elif config.GEMINI_API_KEY:
        _provider = "gemini"
    elif config.GROQ_API_KEY:
        _provider = "groq"
    elif config.OLLAMA_BASE_URL:
        _provider = "ollama"
    else:
        _provider = "demo"
    return _provider


def provider() -> str:
    return detect_provider()


def is_demo() -> bool:
    return provider() == "demo"


def last_error() -> str:
    return _last_error


def model_name() -> str:
    return {
        "anthropic": config.ANTHROPIC_MODEL,
        "cerebras": config.CEREBRAS_MODEL,
        "gemini": config.GEMINI_MODEL,
        "groq": config.GROQ_MODEL,
        "ollama": config.OLLAMA_MODEL,
        "demo": "atlas-demo",
    }[provider()]


async def _anthropic(system: str, prompt: str, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic package is not installed") from exc
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    message = await client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(getattr(block, "text", "") for block in message.content)


async def _openai_compat(base_url: str, api_key: str, model: str, system: str, prompt: str, max_tokens: int) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


def _demo(system: str, prompt: str) -> str:
    lower = f"{system}\n{prompt}".lower()
    if "atlas_json_safety" in lower:
        if any(word in lower for word in ("delete", "production", "payment", "password", "credential")):
            return json.dumps({"score": 75, "factors": ["Potentially consequential action requires human review"]})
        return json.dumps({"score": 8, "factors": ["Read-only or locally sandboxed demo workload"]})
    if "atlas_json_verifier" in lower:
        return json.dumps({"passed": True, "notes": "PASSED · confidence 0.92 — Outputs address the goal and are internally consistent."})
    if "atlas_json_query" in lower:
        goal = _extract(prompt, "GOAL:") or "the requested topic"
        return json.dumps({"query": goal[:240]})
    if "atlas_json_code" in lower:
        if any(word in lower for word in ("statistics", "time series", "average", "mean")):
            return json.dumps({"code": "from statistics import mean, median, pstdev\ndata = [12, 15, 14, 18, 21, 20, 24]\nprint({'count': len(data), 'mean': round(mean(data), 2), 'median': median(data), 'std_dev': round(pstdev(data), 2), 'min': min(data), 'max': max(data)})"})
        return json.dumps({"code": "print('Sandbox task completed successfully')"})
    return (
        "Completed the assigned step in deterministic demo mode. "
        "The result is scoped to the stated goal and recorded for verification."
    )


def _extract(text: str, marker: str) -> str:
    index = text.find(marker)
    if index < 0:
        return ""
    return text[index + len(marker):].split("\n", 1)[0].strip()


async def complete(system: str, prompt: str, max_tokens: int = 1200) -> str:
    global _provider, _last_error
    chosen = provider()
    started = time.perf_counter()
    try:
        if chosen == "demo":
            LLM_CALLS.labels("demo", "ok").inc()
            return _demo(system, prompt)
        text = await _live(chosen, system, prompt, max_tokens)
        LLM_CALLS.labels(chosen, "ok").inc()
        return text
    except Exception as exc:
        LLM_CALLS.labels(chosen, "error").inc()
        _last_error = f"{chosen}: {type(exc).__name__}: {exc}"
        _provider = "demo"
        return _demo(system, prompt)
    finally:
        LLM_LATENCY.labels(chosen).observe(time.perf_counter() - started)


async def _live(chosen: str, system: str, prompt: str, max_tokens: int) -> str:
    if chosen == "anthropic":
        return await _anthropic(system, prompt, max_tokens)
    if chosen == "cerebras":
        return await _openai_compat("https://api.cerebras.ai/v1", config.CEREBRAS_API_KEY, config.CEREBRAS_MODEL, system, prompt, max_tokens)
    if chosen == "gemini":
        return await _openai_compat("https://generativelanguage.googleapis.com/v1beta/openai", config.GEMINI_API_KEY, config.GEMINI_MODEL, system, prompt, max_tokens)
    if chosen == "groq":
        return await _openai_compat("https://api.groq.com/openai/v1", config.GROQ_API_KEY, config.GROQ_MODEL, system, prompt, max_tokens)
    return await _openai_compat(f"{config.OLLAMA_BASE_URL}/v1", "", config.OLLAMA_MODEL, system, prompt, max_tokens)


def _parse_json(text: str) -> dict[str, Any] | list[Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    candidates = [cleaned]
    for opening, closing in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opening), cleaned.rfind(closing)
        if start >= 0 and end > start:
            candidates.append(cleaned[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, (dict, list)):
                return value
        except json.JSONDecodeError:
            continue
    return {}


async def complete_json(system: str, prompt: str, max_tokens: int = 1200) -> dict | list:
    return _parse_json(await complete(system, prompt, max_tokens))
