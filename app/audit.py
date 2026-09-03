"""Append-only audit event storage."""

from __future__ import annotations

import json
import threading

from .config import AUDIT_FILE_ENABLED, AUDIT_LOG
from .models import Event
from .storage import STORE

_lock = threading.Lock()


def record(event: Event) -> None:
    STORE.save_event(event)
    if not AUDIT_FILE_ENABLED:
        return
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _lock, AUDIT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(event.model_dump_json() + "\n")


def tail(limit: int = 200, tenant_id: str = "default") -> list[dict]:
    if limit <= 0:
        return []
    persisted = STORE.audit_tail(tenant_id, limit)
    if persisted:
        return persisted
    if not AUDIT_LOG.exists():
        return []
    lines = AUDIT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    events: list[dict] = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                if value.get("tenant_id", "default") == tenant_id:
                    events.append(value)
        except (json.JSONDecodeError, TypeError):
            continue
    return events
