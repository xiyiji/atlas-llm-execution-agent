"""Fixed-window API rate limiting with Redis and safe local fallback."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from . import config


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app) -> None:
        super().__init__(app)
        self._local: dict[tuple[str, int], int] = defaultdict(int)
        self._lock = threading.Lock()

    @staticmethod
    def _identity(request: Request) -> str:
        api_key = request.headers.get("X-API-Key", "")
        if api_key:
            return hashlib.sha256(api_key.encode()).hexdigest()[:20]
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/api/") or request.url.path == "/api/health":
            return await call_next(request)
        identity = self._identity(request)
        allowed, remaining = await self._check(identity)
        if not allowed:
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(config.RATE_LIMIT_PER_MINUTE)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        return response

    async def _check(self, identity: str) -> tuple[bool, int]:
        bucket = int(time.time() // 60)
        if config.REDIS_URL:
            try:
                return await self._redis_check(identity, bucket)
            except Exception:
                pass
        with self._lock:
            key = (identity, bucket)
            self._local[key] += 1
            count = self._local[key]
            if len(self._local) > 10_000:
                self._local = defaultdict(int, {item: value for item, value in self._local.items() if item[1] >= bucket - 1})
        return count <= config.RATE_LIMIT_PER_MINUTE, config.RATE_LIMIT_PER_MINUTE - count

    async def _redis_check(self, identity: str, bucket: int) -> tuple[bool, int]:
        import redis.asyncio as redis

        client = redis.from_url(config.REDIS_URL)
        key = f"atlas:rate:{identity}:{bucket}"
        try:
            count = int(await client.incr(key))
            if count == 1:
                await client.expire(key, 90)
            return count <= config.RATE_LIMIT_PER_MINUTE, config.RATE_LIMIT_PER_MINUTE - count
        finally:
            await client.aclose()
