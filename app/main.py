"""FastAPI transport layer for Atlas."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import audit, config, llm, memory
from .auth import TenantContext, issue_session, tenant_context
from .event_bus import BUS
from .models import ApprovalRequest, CreateTaskRequest, Event
from .observability import ObservabilityMiddleware, configure_logging, metrics_response
from .orchestrator import ORCHESTRATOR
from .rate_limit import RateLimitMiddleware
from .storage import STORE

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def _approval_janitor() -> None:
    """In-process mode: enforce the approval window even for tasks recovered after a restart."""
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(ORCHESTRATOR.expire_stale_approvals)
        except Exception:
            logging.getLogger(__name__).exception("approval_janitor_failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    config.validate_production()
    await asyncio.to_thread(STORE.initialize)
    status = await llm.probe()
    if status["ready"]:
        logging.getLogger(__name__).info("model_provider_ready", extra={"provider": status["provider"], "model": status["model"]})
    else:
        logging.getLogger(__name__).error("model_provider_not_ready", extra=status)
        print(f"\n!!! Model provider '{status['provider']}' is not ready: {status['problem']}\n!!! {status['hint']}\n", flush=True)
    janitor: asyncio.Task | None = None
    if config.EXECUTION_BACKEND != "celery":
        await ORCHESTRATOR.recover()
        janitor = asyncio.create_task(_approval_janitor())
    try:
        yield
    finally:
        if janitor:
            janitor.cancel()
        await ORCHESTRATOR.shutdown()


app = FastAPI(title="Atlas LLM Execution Agent", version="0.2.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.TRUSTED_HOSTS)
if config.ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.ALLOWED_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
    )
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(ObservabilityMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    checks: dict[str, bool] = {"database": False, "event_bus": False}
    try:
        checks["database"] = await asyncio.to_thread(STORE.healthcheck)
        checks["event_bus"] = await BUS.healthcheck()
    except Exception:
        pass
    status = await llm.probe()
    checks["model_provider"] = status["ready"]
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "provider": status["provider"],
        "model": status["model"],
        "demo": llm.is_demo(),
        "provider_problem": status["problem"],
        "provider_hint": status["hint"],
        "execution_backend": config.EXECUTION_BACKEND,
    }


@app.post("/api/session")
async def create_session(response: Response, tenant: TenantContext = Depends(tenant_context)) -> dict:
    response.set_cookie(
        "atlas_session",
        issue_session(tenant.tenant_id),
        httponly=True,
        secure=config.ENVIRONMENT == "production",
        samesite="strict",
        max_age=config.SESSION_TTL_SECONDS,
        path="/",
    )
    return {"ok": True, "tenant_id": tenant.tenant_id}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()


@app.post("/api/tasks", status_code=202)
async def create_task(request: CreateTaskRequest, tenant: TenantContext = Depends(tenant_context)) -> dict:
    task = ORCHESTRATOR.create_task(request.goal, request.auto_approve, tenant.tenant_id)
    return task.model_dump(mode="json")


@app.get("/api/tasks")
async def list_tasks(limit: int = Query(20, ge=1, le=100), tenant: TenantContext = Depends(tenant_context)) -> list[dict]:
    tasks = await asyncio.to_thread(ORCHESTRATOR.list, tenant.tenant_id, limit)
    return [task.model_dump(mode="json", exclude={"steps", "result"}) for task in tasks]


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, tenant: TenantContext = Depends(tenant_context)) -> dict:
    task = ORCHESTRATOR.get(task_id, tenant.tenant_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.model_dump(mode="json")


@app.post("/api/tasks/{task_id}/approval")
async def approval(task_id: str, request: ApprovalRequest, tenant: TenantContext = Depends(tenant_context)) -> dict:
    if ORCHESTRATOR.get(task_id, tenant.tenant_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not ORCHESTRATOR.resolve_approval(task_id, request.approved, tenant.tenant_id):
        raise HTTPException(status_code=409, detail="Task is not awaiting approval")
    return {"ok": True, "approved": request.approved}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@app.get("/api/tasks/{task_id}/events")
async def events(task_id: str, request: Request, tenant: TenantContext = Depends(tenant_context)) -> StreamingResponse:
    task = ORCHESTRATOR.get(task_id, tenant.tenant_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    queue = ORCHESTRATOR.subscribe(task_id)

    async def gen():
        last_ts = task.updated_at
        seen: set[str] = set()
        try:
            yield _sse("snapshot", {"task": task.model_dump(mode="json")})
            if task.status.value in {"completed", "failed", "denied"}:
                yield _sse("stream.end", {"task_id": task.id, "status": task.status.value})
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event: Event = await asyncio.wait_for(queue.get(), timeout=5)
                except asyncio.TimeoutError:
                    recovered = await asyncio.to_thread(STORE.events_after, task_id, tenant.tenant_id, last_ts, 100)
                    if not recovered:
                        yield ": keepalive\n\n"
                        continue
                    for raw in recovered:
                        replayed = Event.model_validate(raw)
                        last_ts = max(last_ts, replayed.ts)
                        if replayed.id in seen:
                            continue
                        seen.add(replayed.id)
                        yield _sse(replayed.type, replayed.model_dump(mode="json"))
                        if replayed.type == "stream.end":
                            return
                    continue
                last_ts = max(last_ts, event.ts)
                if event.id in seen:
                    continue
                seen.add(event.id)
                yield _sse(event.type, event.model_dump(mode="json"))
                if event.type == "stream.end":
                    break
        finally:
            ORCHESTRATOR.unsubscribe(task_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/audit")
async def audit_events(limit: int = Query(200, ge=1, le=2000), tenant: TenantContext = Depends(tenant_context)) -> list[dict]:
    return audit.tail(limit, tenant.tenant_id)


@app.get("/api/memory")
async def memories(limit: int = Query(20, ge=1, le=100), tenant: TenantContext = Depends(tenant_context)) -> list[dict]:
    return memory.episodic_recall(limit, tenant.tenant_id)
