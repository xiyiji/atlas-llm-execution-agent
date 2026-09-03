"""Durable lifecycle: restart recovery, worker-mode approval pause/resume, expiry, concurrency."""

import asyncio
import json
import time

import pytest

from app import config, memory, task_queue
from app.models import PlanStep, RiskAssessment, RiskLevel, StepStatus, Task, TaskStatus
from app.orchestrator import Orchestrator
from app.storage import STORE

TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DENIED}


async def _wait_until(predicate, timeout: float = 6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _low_risk() -> RiskAssessment:
    return RiskAssessment(score=5, level=RiskLevel.LOW, factors=["read-only"], requires_approval=False)


def _high_risk() -> RiskAssessment:
    return RiskAssessment(score=80, level=RiskLevel.HIGH, factors=["deletion"], requires_approval=True)


def test_recover_resumes_unfinished_task_without_rerunning_completed_steps():
    async def scenario():
        task = Task(goal="Prepare a concise project summary", tenant_id="recover")
        task.status = TaskStatus.RUNNING
        task.risk = _low_risk()
        task.steps = [
            PlanStep(title="Already done before the crash", agent="planner", status=StepStatus.COMPLETED, output="kept", attempts=1),
            PlanStep(title="Was mid-flight when the process died", agent="planner", status=StepStatus.RUNNING, attempts=1),
            PlanStep(title="Verify outputs against the goal", agent="verifier"),
        ]
        STORE.save_task(task)

        orchestrator = Orchestrator()
        recovered = await orchestrator.recover()
        assert task.id in recovered
        live = orchestrator.tasks[task.id]
        assert await _wait_until(lambda: live.status in TERMINAL)
        assert live.status == TaskStatus.COMPLETED, live.error
        assert live.steps[0].attempts == 1 and live.steps[0].output == "kept"
        assert live.steps[1].attempts == 2  # one attempt before the crash, one after
        events = [row["type"] for row in STORE.events_after(task.id, "recover")]
        assert "task.recovered" in events and events[-1] == "stream.end"

    asyncio.run(scenario())


def test_recovered_awaiting_approval_task_still_honours_the_decision():
    async def scenario():
        task = Task(goal="Delete records from the production database", tenant_id="recover")
        task.status = TaskStatus.AWAITING_APPROVAL
        task.risk = _high_risk()
        task.steps = [PlanStep(title="Draft procedure", agent="planner"), PlanStep(title="Verify outputs against the goal", agent="verifier")]
        STORE.save_task(task)

        orchestrator = Orchestrator()
        await orchestrator.recover()
        live = orchestrator.tasks[task.id]
        await asyncio.sleep(0.05)
        assert live.status == TaskStatus.AWAITING_APPROVAL
        assert orchestrator.resolve_approval(task.id, True, "recover")
        assert await _wait_until(lambda: live.status in TERMINAL)
        assert live.status == TaskStatus.COMPLETED, live.error

    asyncio.run(scenario())


def test_worker_mode_pauses_for_approval_without_holding_the_worker(monkeypatch):
    queued: list[str] = []
    monkeypatch.setattr(config, "EXECUTION_BACKEND", "celery")
    monkeypatch.setattr(task_queue, "enqueue_task", queued.append)

    async def scenario():
        orchestrator = Orchestrator()
        task = orchestrator.create_task("Delete records from the production database", False, "worker")
        assert queued == [task.id]

        # A worker picks the task up: it plans, scores risk, pauses durably, and returns.
        await asyncio.wait_for(orchestrator.resume_task(task.id), timeout=5)
        persisted = STORE.get_task(task.id, "worker")
        assert persisted.status == TaskStatus.AWAITING_APPROVAL
        assert persisted.approval_decision is None
        assert persisted.steps and persisted.risk.requires_approval

        # Approval from the API re-enqueues; the next worker continues from the stored plan.
        assert orchestrator.resolve_approval(task.id, True, "worker")
        assert queued == [task.id, task.id]
        await asyncio.wait_for(orchestrator.resume_task(task.id), timeout=5)
        finished = STORE.get_task(task.id, "worker")
        assert finished.status == TaskStatus.COMPLETED, finished.error
        assert finished.result
        assert [row["type"] for row in STORE.events_after(task.id, "worker")].count("plan.created") == 1

        # Running the same finished task again is a no-op.
        await orchestrator.resume_task(task.id)
        assert STORE.get_task(task.id, "worker").updated_at == finished.updated_at

    asyncio.run(scenario())


def test_worker_mode_denial_finishes_without_a_worker(monkeypatch):
    queued: list[str] = []
    monkeypatch.setattr(config, "EXECUTION_BACKEND", "celery")
    monkeypatch.setattr(task_queue, "enqueue_task", queued.append)

    async def scenario():
        orchestrator = Orchestrator()
        task = orchestrator.create_task("Delete records from the production database", False, "worker")
        await orchestrator.resume_task(task.id)
        assert orchestrator.resolve_approval(task.id, False, "worker")
        assert queued == [task.id]
        denied = STORE.get_task(task.id, "worker")
        assert denied.status == TaskStatus.DENIED
        assert not orchestrator.resolve_approval(task.id, True, "worker")

    asyncio.run(scenario())


def test_worker_mode_queue_failure_marks_task_failed(monkeypatch):
    monkeypatch.setattr(config, "EXECUTION_BACKEND", "celery")

    def broken(_task_id: str) -> None:
        raise ConnectionError("broker down")

    monkeypatch.setattr(task_queue, "enqueue_task", broken)
    orchestrator = Orchestrator()
    with pytest.raises(ConnectionError):
        orchestrator.create_task("Prepare a concise project summary", False, "worker")
    failed = STORE.list_tasks(tenant_id="worker", statuses=["failed"], limit=1)[0]
    assert "Queue dispatch failed" in failed.error


def test_stale_approvals_expire(monkeypatch):
    monkeypatch.setattr(config, "APPROVAL_TIMEOUT_SECONDS", 100)
    task = Task(goal="Delete records", tenant_id="expiry", status=TaskStatus.AWAITING_APPROVAL, risk=_high_risk())
    task.updated_at = time.time() - 1000
    STORE.save_task(task)
    fresh = Task(goal="Delete records", tenant_id="expiry", status=TaskStatus.AWAITING_APPROVAL, risk=_high_risk())
    STORE.save_task(fresh)

    orchestrator = Orchestrator()
    expired = orchestrator.expire_stale_approvals()
    assert task.id in expired and fresh.id not in expired
    assert STORE.get_task(task.id).status == TaskStatus.DENIED
    assert STORE.get_task(fresh.id).status == TaskStatus.AWAITING_APPROVAL
    assert any(row["type"] == "approval.timeout" for row in STORE.events_after(task.id, "expiry"))


def test_concurrent_tasks_complete_and_memory_file_stays_valid():
    async def scenario():
        orchestrator = Orchestrator()
        goals = ["Prepare a concise project summary", "Compute basic statistics for a sample time series", "Research the latest agent frameworks"]
        tasks = [orchestrator.create_task(goals[i % len(goals)], False, "concurrency") for i in range(12)]
        assert await _wait_until(lambda: all(task.status in TERMINAL for task in tasks), timeout=20)
        assert all(task.status == TaskStatus.COMPLETED for task in tasks), [task.error for task in tasks]
        assert len({task.id for task in tasks}) == 12
        persisted = STORE.list_tasks(tenant_id="concurrency", statuses=["completed"], limit=100)
        assert {task.id for task in tasks} <= {task.id for task in persisted}
        if config.MEMORY_FILE_ENABLED:
            items = json.loads(config.MEMORY_FILE.read_text(encoding="utf-8"))
            assert isinstance(items, list) and len(items) <= 100
        assert len(memory.episodic_recall(20, "concurrency")) >= 12

    asyncio.run(scenario())


def test_shutdown_cancels_runners_and_leaves_state_resumable():
    async def scenario():
        orchestrator = Orchestrator()
        task = orchestrator.create_task("Delete records from the production database", False, "shutdown")
        assert await _wait_until(lambda: task.status == TaskStatus.AWAITING_APPROVAL)
        await orchestrator.shutdown()
        persisted = STORE.get_task(task.id, "shutdown")
        assert persisted.status == TaskStatus.AWAITING_APPROVAL
        assert not orchestrator._runners

    asyncio.run(scenario())
