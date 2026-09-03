"""Local fan-out with optional Redis Pub/Sub for multi-instance SSE."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict

from . import config
from .models import Event

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self.instance_id = uuid.uuid4().hex
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)
        self._listeners: dict[int, asyncio.Task] = {}

    def publish(self, event: Event) -> None:
        for queue in tuple(self._subscribers.get(event.task_id, [])):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("sse_subscriber_queue_full", extra={"task_id": event.task_id})
        if config.REDIS_URL:
            try:
                asyncio.get_running_loop().create_task(self._publish_redis(event))
            except RuntimeError:
                log.warning("redis_publish_skipped_no_event_loop", extra={"task_id": event.task_id})

    async def _publish_redis(self, event: Event) -> None:
        try:
            import redis.asyncio as redis

            client = redis.from_url(config.REDIS_URL, decode_responses=True)
            payload = json.dumps({"origin": self.instance_id, "event": event.model_dump(mode="json")})
            await client.publish(f"atlas:task:{event.task_id}", payload)
            await client.aclose()
        except Exception:
            log.exception("redis_publish_failed", extra={"task_id": event.task_id})

    def subscribe(self, task_id: str) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._subscribers[task_id].append(queue)
        if config.REDIS_URL:
            self._listeners[id(queue)] = asyncio.get_running_loop().create_task(self._listen_redis(task_id, queue))
        return queue

    async def _listen_redis(self, task_id: str, queue: asyncio.Queue[Event]) -> None:
        try:
            import redis.asyncio as redis

            client = redis.from_url(config.REDIS_URL, decode_responses=True)
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(f"atlas:task:{task_id}")
            try:
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    payload = json.loads(message["data"])
                    if payload.get("origin") == self.instance_id:
                        continue
                    event = Event.model_validate(payload["event"])
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        log.warning("sse_subscriber_queue_full", extra={"task_id": task_id})
            finally:
                await pubsub.aclose()
                await client.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("redis_subscription_failed", extra={"task_id": task_id})

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[Event]) -> None:
        subscribers = self._subscribers.get(task_id, [])
        if queue in subscribers:
            subscribers.remove(queue)
        if not subscribers:
            self._subscribers.pop(task_id, None)
        listener = self._listeners.pop(id(queue), None)
        if listener:
            listener.cancel()

    async def healthcheck(self) -> bool:
        if not config.REDIS_URL:
            return True
        import redis.asyncio as redis

        client = redis.from_url(config.REDIS_URL)
        try:
            return bool(await client.ping())
        finally:
            await client.aclose()


BUS = EventBus()
