"""HTTP surface: auth, tenant isolation, rate limiting, SSE, approval flow, operational endpoints."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.rate_limit import RateLimitMiddleware


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "AUTH_REQUIRED", True)
    monkeypatch.setattr(config, "API_KEYS", "alpha:key-alpha,beta:key-beta")
    monkeypatch.setattr(config, "SESSION_SECRET", "s" * 40)
    with TestClient(main.app) as test_client:
        yield test_client


def _wait_terminal(client: TestClient, task_id: str, headers: dict, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        if task["status"] in {"completed", "failed", "denied"}:
            return task
        time.sleep(0.02)
    raise AssertionError("task did not finish")


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if len(lines) < 2:
            continue
        name = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((name, data))
    return events


def test_requests_without_credentials_are_rejected(client):
    assert client.get("/api/memory").status_code == 401
    assert client.post("/api/tasks", json={"goal": "hello"}).status_code == 401
    assert client.get("/api/health").status_code == 200  # liveness stays open
    assert client.get("/metrics").status_code == 200


def test_tenants_cannot_see_each_others_tasks(client):
    alpha = {"X-API-Key": "key-alpha"}
    beta = {"X-API-Key": "key-beta"}
    created = client.post("/api/tasks", json={"goal": "Prepare a concise project summary"}, headers=alpha)
    assert created.status_code == 202
    task_id = created.json()["id"]
    assert created.json()["tenant_id"] == "alpha"

    assert client.get(f"/api/tasks/{task_id}", headers=beta).status_code == 404
    assert client.get(f"/api/tasks/{task_id}/events", headers=beta).status_code == 404
    assert client.post(f"/api/tasks/{task_id}/approval", json={"approved": True}, headers=beta).status_code == 404

    finished = _wait_terminal(client, task_id, alpha)
    assert finished["status"] == "completed", finished["error"]
    assert task_id in {item["id"] for item in client.get("/api/tasks", headers=alpha).json()}
    assert task_id not in {item["id"] for item in client.get("/api/tasks", headers=beta).json()}
    assert all(row["tenant_id"] == "beta" for row in client.get("/api/audit", headers=beta).json())
    assert any(row["task_id"] == task_id for row in client.get("/api/audit", headers=alpha).json())


def test_session_cookie_replaces_api_key(client):
    response = client.post("/api/session", headers={"X-API-Key": "key-beta"})
    assert response.status_code == 200 and response.json()["tenant_id"] == "beta"
    assert "atlas_session" in response.cookies
    cookie_client = client  # TestClient keeps the cookie jar
    created = cookie_client.post("/api/tasks", json={"goal": "Prepare a concise project summary"})
    assert created.status_code == 202 and created.json()["tenant_id"] == "beta"


def test_approval_flow_over_http_with_sse(client):
    alpha = {"X-API-Key": "key-alpha"}
    created = client.post("/api/tasks", json={"goal": "Delete records from the production database"}, headers=alpha).json()
    task_id = created["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get(f"/api/tasks/{task_id}", headers=alpha).json()["status"] != "awaiting_approval":
        time.sleep(0.02)
    assert client.get(f"/api/tasks/{task_id}", headers=alpha).json()["status"] == "awaiting_approval"

    decision = client.post(f"/api/tasks/{task_id}/approval", json={"approved": True}, headers=alpha)
    assert decision.status_code == 200
    assert client.post(f"/api/tasks/{task_id}/approval", json={"approved": True}, headers=alpha).status_code == 409

    finished = _wait_terminal(client, task_id, alpha)
    assert finished["status"] == "completed", finished["error"]

    with client.stream("GET", f"/api/tasks/{task_id}/events", headers=alpha) as stream:
        events = _sse_events(stream.read().decode())
    assert events[0][0] == "snapshot" and events[0][1]["task"]["status"] == "completed"
    assert events[-1][0] == "stream.end"


def test_sse_streams_live_events_until_end(client, monkeypatch):
    import asyncio

    from app.agents import COMMITTEE

    planner = COMMITTEE["planner"]
    original = planner.run

    async def slow(task, step):
        await asyncio.sleep(0.4)
        return await original(task, step)

    monkeypatch.setattr(planner, "run", slow)
    alpha = {"X-API-Key": "key-alpha"}
    task_id = client.post("/api/tasks", json={"goal": "Prepare a concise project summary"}, headers=alpha).json()["id"]
    with client.stream("GET", f"/api/tasks/{task_id}/events", headers=alpha) as stream:
        events = _sse_events(stream.read().decode())
    names = [name for name, _ in events]
    assert names[0] == "snapshot" and names[-1] == "stream.end"
    assert "step.completed" in names and "task.completed" in names
    assert all(data.get("tenant_id", "alpha") == "alpha" for _, data in events[1:])
    assert len({data["id"] for name, data in events[1:] if "id" in data}) == len(events) - 1  # no duplicates


def test_rate_limit_and_request_size(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 3)
    for middleware in main.app.user_middleware:
        if middleware.cls is RateLimitMiddleware:
            break
    headers = {"X-API-Key": "key-beta-limited"}
    statuses = [client.get("/api/memory", headers=headers).status_code for _ in range(5)]
    assert statuses[-1] == 429 and statuses.count(429) >= 2
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 100)
    too_big = client.post("/api/tasks", json={"goal": "x" * 500}, headers={"X-API-Key": "key-alpha"})
    assert too_big.status_code == 413


def test_security_headers_and_request_id(client):
    response = client.get("/api/health", headers={"X-Request-ID": "req-123"})
    assert response.headers["X-Request-ID"] == "req-123"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in response.headers
    body = response.json()
    assert body["ok"] and body["checks"]["database"]
