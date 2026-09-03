"""Provider-neutral language-model access.

Demo mode (FORCE_DEMO=1) never touches the network. With a live provider, a
failed call raises ``ProviderError`` with a message that says what to fix; the
task then fails visibly instead of quietly finishing with made-up output.
Set LLM_FALLBACK_TO_DEMO=1 only if you explicitly want the old behaviour.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx
from prometheus_client import Counter, Histogram

from . import config

log = logging.getLogger(__name__)

LLM_CALLS = Counter("atlas_llm_calls_total", "Model calls by provider and outcome", ["provider", "outcome"])
LLM_LATENCY = Histogram("atlas_llm_call_duration_seconds", "Model call latency", ["provider"])


_provider: str | None = None
_last_error = ""


class ProviderError(RuntimeError):
    """A live model call failed. ``hint`` says what the operator should do."""

    def __init__(self, provider_name: str, detail: str, hint: str) -> None:
        super().__init__(f"Model provider '{provider_name}' failed: {detail}. {hint}")
        self.provider = provider_name
        self.detail = detail
        self.hint = hint


class ModelOutputError(RuntimeError):
    """The model answered, but not with the JSON the caller asked for. Retryable at the step level."""


def _hint(provider_name: str, detail: str) -> str:
    lower = detail.lower()
    if provider_name == "ollama":
        if "404" in lower or "not found" in lower:
            return f"Ollama has no model named '{config.OLLAMA_MODEL}'. Run `ollama pull {config.OLLAMA_MODEL}` or set OLLAMA_MODEL to a model you have (`ollama list`)."
        if "connect" in lower or "refused" in lower or "timed out" in lower:
            return f"Ollama is not reachable at {config.OLLAMA_BASE_URL}. Start it with `ollama serve`, or set an API key (ANTHROPIC_API_KEY / GROQ_API_KEY / ...) in .env, or set FORCE_DEMO=1."
        return "Check that Ollama is running and the model is pulled, or switch provider in .env."
    if "401" in lower or "403" in lower or "authentication" in lower or "api key" in lower:
        return f"The {provider_name} API key was rejected. Check the key in .env."
    if "429" in lower or "rate" in lower:
        return f"{provider_name} is rate-limiting this key. Wait and retry, or use another key."
    if "connect" in lower or "timed out" in lower or "name resolution" in lower:
        return f"Could not reach {provider_name}. Check network access from this machine."
    return "Check the provider settings in .env, or set FORCE_DEMO=1 to run without a model."


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


async def _openai_compat(base_url: str, api_key: str, model: str, system: str, prompt: str, max_tokens: int, json_mode: bool = False) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    if json_mode:
        # Ollama, Groq, Cerebras and Gemini's OpenAI endpoint all honour this; it stops small models from writing prose around the JSON.
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"] or ""


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


async def complete(system: str, prompt: str, max_tokens: int = 1200, json_mode: bool = False) -> str:
    global _provider, _last_error
    chosen = provider()
    started = time.perf_counter()
    try:
        if chosen == "demo":
            LLM_CALLS.labels("demo", "ok").inc()
            return _demo(system, prompt)
        text = await _live(chosen, system, prompt, max_tokens, json_mode)
        LLM_CALLS.labels(chosen, "ok").inc()
        _last_error = ""
        return text
    except Exception as exc:
        LLM_CALLS.labels(chosen, "error").inc()
        detail = _describe(exc)
        _last_error = f"{chosen}: {detail}"
        if config.LLM_FALLBACK_TO_DEMO:
            _provider = "demo"
            return _demo(system, prompt)
        raise ProviderError(chosen, detail, _hint(chosen, detail)) from exc
    finally:
        LLM_LATENCY.labels(chosen).observe(time.perf_counter() - started)


def _describe(exc: Exception) -> str:
    """Short, useful error text: HTTP status plus the provider's own message when there is one."""
    response = getattr(exc, "response", None)
    if response is not None:
        body = ""
        try:
            payload = response.json()
            body = payload.get("error", payload) if isinstance(payload, dict) else payload
            if isinstance(body, dict):
                body = body.get("message", json.dumps(body))
        except Exception:
            body = (response.text or "")[:200]
        return f"HTTP {response.status_code} {str(body)[:200]}".strip()
    return f"{type(exc).__name__}: {exc}"[:300]


async def probe() -> dict:
    """Cheap readiness check for the configured provider, used by /api/health and at startup."""
    chosen = provider()
    result = {"provider": chosen, "model": model_name(), "ready": True, "problem": "", "hint": ""}
    if chosen == "demo":
        return result
    try:
        if chosen == "ollama":
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/tags")
                response.raise_for_status()
                names = {item.get("name", "") for item in response.json().get("models", [])}
            wanted = config.OLLAMA_MODEL
            if wanted not in names and f"{wanted}:latest" not in names:
                raise RuntimeError(f"model '{wanted}' not found (available: {', '.join(sorted(names)) or 'none'})")
        elif not {
            "anthropic": config.ANTHROPIC_API_KEY,
            "cerebras": config.CEREBRAS_API_KEY,
            "gemini": config.GEMINI_API_KEY,
            "groq": config.GROQ_API_KEY,
        }.get(chosen):
            raise RuntimeError("API key is empty")
    except Exception as exc:
        detail = _describe(exc)
        result.update(ready=False, problem=detail, hint=_hint(chosen, detail))
    return result


async def _live(chosen: str, system: str, prompt: str, max_tokens: int, json_mode: bool) -> str:
    if chosen == "anthropic":
        return await _anthropic(system, prompt, max_tokens)
    if chosen == "cerebras":
        return await _openai_compat("https://api.cerebras.ai/v1", config.CEREBRAS_API_KEY, config.CEREBRAS_MODEL, system, prompt, max_tokens, json_mode)
    if chosen == "gemini":
        return await _openai_compat("https://generativelanguage.googleapis.com/v1beta/openai", config.GEMINI_API_KEY, config.GEMINI_MODEL, system, prompt, max_tokens, json_mode)
    if chosen == "groq":
        return await _openai_compat("https://api.groq.com/openai/v1", config.GROQ_API_KEY, config.GROQ_MODEL, system, prompt, max_tokens, json_mode)
    return await _openai_compat(f"{config.OLLAMA_BASE_URL}/v1", "", config.OLLAMA_MODEL, system, prompt, max_tokens, json_mode)


def _parse_json(text: str) -> dict[str, Any] | list[Any]:
    """Pull the first JSON object/array out of a model reply; {} if there is none."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    cleaned = re.sub(r"```(?:json)?", "", cleaned)
    decoder = json.JSONDecoder()
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
    # Prose before/after, or several objects: decode from each opening bracket and keep the first that parses.
    for index, char in enumerate(cleaned):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(cleaned[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, (dict, list)):
                return value
    return {}


async def complete_json(system: str, prompt: str, max_tokens: int = 1200) -> dict | list:
    """Ask for JSON. One strict retry if the reply is not JSON; then ModelOutputError so the step can retry or fail visibly."""
    text = await complete(system, prompt, max_tokens, json_mode=True)
    value = _parse_json(text)
    if value:
        return value
    log.warning("model_reply_not_json", extra={"provider": provider(), "reply": text[:400]})
    strict = f"{system}\nReply with exactly one JSON object and nothing else: no prose, no markdown, no explanation."
    text = await complete(strict, prompt, max_tokens, json_mode=True)
    value = _parse_json(text)
    if value:
        return value
    snippet = (text or "").strip().replace("\n", " ")[:160]
    raise ModelOutputError(f"{provider()}/{model_name()} did not return JSON after two attempts; last reply: {snippet!r}")
