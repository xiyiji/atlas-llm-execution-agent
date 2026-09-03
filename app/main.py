"""FastAPI transport layer for Atlas."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import audit, llm, memory
from .models import ApprovalRequest, CreateTaskRequest, Event
from .orchestrator import ORCHESTRATOR


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="Atlas LLM Execution Agent", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "provider": llm.provider(), "model": llm.model_name(), "demo": llm.is_demo()}


@app.post("/api/tasks", status_code=202)
async def create_task(request: CreateTaskRequest) -> dict:
    task = ORCHESTRATOR.create_task(request.goal, request.auto_approve)
    return task.model_dump(mode="json")


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = ORCHESTRATOR.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.model_dump(mode="json")


@app.post("/api/tasks/{task_id}/approval")
async def approval(task_id: str, request: ApprovalRequest) -> dict:
    if ORCHESTRATOR.get(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not ORCHESTRATOR.resolve_approval(task_id, request.approved):
        raise HTTPException(status_code=409, detail="Task is not awaiting approval")
    return {"ok": True, "approved": request.approved}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@app.get("/api/tasks/{task_id}/events")
async def events(task_id: str, request: Request) -> StreamingResponse:
    task = ORCHESTRATOR.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    queue = ORCHESTRATOR.subscribe(task_id)

    async def gen():
        try:
            yield _sse("snapshot", {"task": task.model_dump(mode="json")})
            if task.status.value in {"completed", "failed", "denied"}:
                yield _sse("stream.end", {"task_id": task.id, "status": task.status.value})
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event: Event = await asyncio.wait_for(queue.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event.type, event.model_dump(mode="json"))
                if event.type == "stream.end":
                    break
        finally:
            ORCHESTRATOR.unsubscribe(task_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/audit")
async def audit_events(limit: int = Query(200, ge=1, le=2000)) -> list[dict]:
    return audit.tail(limit)


@app.get("/api/memory")
async def memories(limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    return memory.episodic_recall(limit)
