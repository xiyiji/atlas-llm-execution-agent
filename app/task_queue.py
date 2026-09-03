"""Celery worker entrypoint for durable out-of-process execution."""

from __future__ import annotations

import asyncio

from celery import Celery
from redis import Redis

from . import config

celery_app = Celery("atlas", broker=config.CELERY_BROKER_URL, backend=config.CELERY_RESULT_BACKEND)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    beat_schedule={
        "atlas-expire-approvals": {
            "task": "atlas.expire_approvals",
            "schedule": 60.0,
        },
    },
)


def _execution_lock(task_id: str):
    client = Redis.from_url(config.CELERY_BROKER_URL)
    return client, client.lock(f"atlas:execution-lock:{task_id}", timeout=3600, blocking_timeout=0)


@celery_app.task(name="atlas.execute_task", autoretry_for=(ConnectionError,), retry_backoff=True, max_retries=3)
def execute_task(task_id: str) -> None:
    """Run (or resume) one task. A per-task Redis lock keeps redeliveries from running it twice."""
    from .orchestrator import ORCHESTRATOR

    client, lock = _execution_lock(task_id)
    if not lock.acquire(blocking=False):
        client.close()
        return
    try:
        asyncio.run(ORCHESTRATOR.resume_task(task_id))
    finally:
        try:
            lock.release()
        finally:
            client.close()


@celery_app.task(name="atlas.expire_approvals")
def expire_approvals() -> list[str]:
    """Beat job: deny tasks whose approval window has passed."""
    from .orchestrator import ORCHESTRATOR

    return ORCHESTRATOR.expire_stale_approvals()


def enqueue_task(task_id: str) -> None:
    execute_task.delay(task_id)
