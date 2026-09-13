"""Exercise the production Compose path from authenticated API request to worker result."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8000"
HEADERS = {"X-API-Key": "ci-atlas-key"}


def request(path: str, *, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = dict(HEADERS)
    if body is not None:
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(f"{BASE_URL}{path}", data=body, headers=headers), timeout=10) as response:
        return json.load(response)


def wait_ready(timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/api/ready", timeout=5) as response:
                if response.status == 200 and json.load(response)["ok"]:
                    return
        except (OSError, urllib.error.HTTPError):
            pass
        time.sleep(2)
    raise TimeoutError("Atlas API did not become ready")


def main() -> int:
    wait_ready()
    created = request(
        "/api/tasks",
        payload={"goal": "Write and run Python to compute statistics for a sample time series"},
    )
    task_id = created["id"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        task = request(f"/api/tasks/{task_id}")
        if task["status"] in {"completed", "failed", "denied"}:
            assert task["status"] == "completed", task
            assert task["verified"] is True, task
            assert any(step["agent"] == "coder" and step["status"] == "completed" for step in task["steps"]), task
            print(json.dumps({"task_id": task_id, "status": task["status"], "verified": task["verified"]}))
            return 0
        time.sleep(1)
    raise TimeoutError(f"Task {task_id} did not complete")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"compose smoke test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
