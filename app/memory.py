"""Per-task working memory and durable episodic memory."""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict

from .config import MEMORY_FILE, MEMORY_FILE_ENABLED
from .storage import STORE

_working: dict[str, list[dict]] = defaultdict(list)
_working_lock = threading.Lock()
_episodic_lock = threading.Lock()


def working_write(task_id: str, agent: str, note: str) -> None:
    with _working_lock:
        _working[task_id].append({"ts": time.time(), "agent": agent, "note": str(note)})


def working_read(task_id: str) -> list[dict]:
    with _working_lock:
        return [dict(item) for item in _working.get(task_id, [])]


def working_context(task_id: str, max_notes: int = 6) -> str:
    notes = working_read(task_id)[-max(0, max_notes):]
    return "\n".join(f"[{item['agent']}] {item['note']}" for item in notes)


def working_clear(task_id: str) -> None:
    with _working_lock:
        _working.pop(task_id, None)


def _read_episodic() -> list[dict]:
    if not MEMORY_FILE.exists():
        return []
    try:
        value = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def episodic_store(goal: str, outcome: str, summary: str, tenant_id: str = "default") -> None:
    now = time.time()
    STORE.store_memory(tenant_id, now, goal, outcome, summary)
    if not MEMORY_FILE_ENABLED:
        return
    with _episodic_lock:
        items = _read_episodic()
        items.append({"ts": now, "tenant_id": tenant_id, "goal": goal, "outcome": outcome, "summary": summary})
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = MEMORY_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(items[-100:], ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(MEMORY_FILE)


def episodic_recall(limit: int = 5, tenant_id: str = "default") -> list[dict]:
    if limit <= 0:
        return []
    persisted = STORE.recall_memory(tenant_id, limit)
    if persisted:
        return persisted
    with _episodic_lock:
        items = [item for item in _read_episodic() if item.get("tenant_id", "default") == tenant_id]
        return items[-limit:]
