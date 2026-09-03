"""Structured logging and Prometheus instrumentation."""

from __future__ import annotations

import logging
import re
import time
import uuid

from fastapi import Request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pythonjsonlogger.json import JsonFormatter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from . import config

REQUESTS = Counter("atlas_http_requests_total", "HTTP requests", ["method", "path", "status"])
REQUEST_LATENCY = Histogram("atlas_http_request_duration_seconds", "HTTP request latency", ["method", "path"])
TASK_EVENTS = Counter("atlas_task_events_total", "Orchestrator events", ["type", "agent"])
ACTIVE_REQUESTS = Gauge("atlas_http_active_requests", "Requests currently being served")


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(config.LOG_LEVEL)


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        route = re.sub(r"/api/tasks/[^/]+", "/api/tasks/{task_id}", request.url.path)
        started = time.perf_counter()
        ACTIVE_REQUESTS.inc()
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > config.MAX_REQUEST_BYTES:
            ACTIVE_REQUESTS.dec()
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        try:
            response = await call_next(request)
        except Exception:
            REQUESTS.labels(request.method, route, "500").inc()
            raise
        finally:
            ACTIVE_REQUESTS.dec()
            REQUEST_LATENCY.labels(request.method, route).observe(time.perf_counter() - started)
        REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:"
        return response


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
