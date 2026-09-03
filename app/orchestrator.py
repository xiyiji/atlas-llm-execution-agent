"""Control-plane for the complete Atlas task lifecycle.

The orchestrator owns state transitions, event emission, approval gates,
retries, and rework. It never performs model inference itself.

Execution modes
---------------
* ``inprocess`` — tasks run as asyncio tasks inside the API process. State is
  persisted after every event, so unfinished tasks are picked up again on the
  next start (see :meth:`Orchestrator.recover`).
* ``celery`` — tasks are enqueued to Celery workers. A high-risk task pauses
  durably in ``awaiting_approval`` and releases its worker; the approval
  endpoint re-enqueues it once a reviewer decides.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import audit, config, llm, memory
from .agents import COMMITTEE
from .event_bus import BUS
from .models import Event, PlanStep, StepStatus, Task, TaskStatus
from .observability import TASK_EVENTS
from .storage import STORE

log = logging.getLogger(__name__)

TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DENIED}
ACTIVE_STATUSES = [status.value for status in TaskStatus if status not in TERMINAL_STATUSES]
MAX_ROUNDS = 2  # original pass + a single rework round


class Orchestrator:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self._runners: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ public

    def create_task(self, goal: str, auto_approve: bool = False, tenant_id: str = "default") -> Task:
        task = Task(goal=goal.strip(), auto_approve=auto_approve, tenant_id=tenant_id)
        self.tasks[task.id] = task
        self._emit(task, "task.created", "", "Task accepted", {"goal": task.goal, "auto_approve": auto_approve})
        self._dispatch(task)
        return task

    def get(self, task_id: str, tenant_id: str | None = None) -> Task | None:
        task = None if config.EXECUTION_BACKEND == "celery" else self.tasks.get(task_id)
        if task is None:
            task = STORE.get_task(task_id, tenant_id)
        if task is not None and tenant_id is not None and task.tenant_id != tenant_id:
            return None
        return task

    def list(self, tenant_id: str, limit: int = 50) -> list[Task]:
        return STORE.list_tasks(tenant_id=tenant_id, limit=limit)

    def resolve_approval(self, task_id: str, approved: bool, tenant_id: str = "default") -> bool:
        task = self.get(task_id, tenant_id)
        if not task or task.status != TaskStatus.AWAITING_APPROVAL or task.approval_decision is not None:
            return False
        task.approval_decision = bool(approved)
        self.tasks[task.id] = task
        self._emit(task, "approval.resolved", "safety", "Plan approved" if approved else "Plan denied", {"approved": approved})
        if config.EXECUTION_BACKEND == "celery":
            # The waiting worker was released when the task paused; hand the task back to the queue.
            if approved:
                self._dispatch(task)
            else:
                self._deny(task, "Human reviewer denied the plan.")
        return True

    async def resume_task(self, task_id: str) -> None:
        """Worker entrypoint: load the persisted task and continue from where it stopped."""
        task = STORE.get_task(task_id)
        if task is None:
            raise LookupError(f"Task not found: {task_id}")
        if task.status in TERMINAL_STATUSES:
            return
        self.tasks[task.id] = task
        await self._run(task)

    async def recover(self) -> list[str]:
        """Re-attach unfinished tasks after a process restart (in-process mode only)."""
        if config.EXECUTION_BACKEND == "celery":
            return []
        recovered: list[str] = []
        for task in STORE.list_tasks(statuses=ACTIVE_STATUSES, limit=500):
            self.tasks[task.id] = task
            self._emit(task, "task.recovered", "", "Resuming after service restart", {"status": task.status.value})
            self._schedule(task)
            recovered.append(task.id)
        if recovered:
            log.info("tasks_recovered", extra={"count": len(recovered)})
        return recovered

    def expire_stale_approvals(self, now: float | None = None) -> list[str]:
        """Deny tasks that waited longer than APPROVAL_TIMEOUT_SECONDS. Safe to call from a scheduler."""
        now = now or time.time()
        expired: list[str] = []
        for task in STORE.list_tasks(statuses=[TaskStatus.AWAITING_APPROVAL.value], limit=500):
            if task.approval_decision is not None or now - task.updated_at < config.APPROVAL_TIMEOUT_SECONDS:
                continue
            self.tasks[task.id] = task
            task.approval_decision = False
            self._emit(task, "approval.timeout", "safety", "Approval window expired", {})
            self._deny(task, "Approval window expired before a reviewer decided.")
            expired.append(task.id)
        return expired

    def subscribe(self, task_id: str) -> asyncio.Queue[Event]:
        return BUS.subscribe(task_id)

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[Event]) -> None:
        BUS.unsubscribe(task_id, queue)

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Stop in-flight runners; their persisted state is resumed on the next start."""
        runners = [runner for runner in self._runners if not runner.done()]
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.wait(runners, timeout=timeout)

    # ---------------------------------------------------------------- dispatch

    def _dispatch(self, task: Task) -> None:
        if config.EXECUTION_BACKEND == "celery":
            from .task_queue import enqueue_task

            try:
                enqueue_task(task.id)
            except Exception as exc:
                task.error = f"Queue dispatch failed: {type(exc).__name__}: {exc}"
                self._set_status(task, TaskStatus.FAILED)
                self._emit(task, "task.failed", "", task.error, {"error": task.error})
                self._emit(task, "stream.end", "", "Task failed", {"status": task.status.value})
                raise
            self._emit(task, "task.queued", "", "Queued for a worker", {})
        else:
            self._schedule(task)

    def _schedule(self, task: Task) -> None:
        runner = asyncio.get_running_loop().create_task(self._run(task))
        self._runners.add(runner)
        runner.add_done_callback(self._runners.discard)

    # ------------------------------------------------------------------ events

    def _emit(self, task: Task, type_: str, agent: str, message: str, data: dict | None = None) -> Event:
        task.updated_at = time.time()
        event = Event(task_id=task.id, tenant_id=task.tenant_id, type=type_, agent=agent, message=message, data=data or {})
        STORE.save_task(task)
        audit.record(event)
        BUS.publish(event)
        TASK_EVENTS.labels(type_, agent or "system").inc()
        return event

    def _set_status(self, task: Task, status: TaskStatus) -> None:
        task.status = status
        self._emit(task, "task.status", "", status.value, {"status": status.value})

    def _finish(self, task: Task, status: TaskStatus, outcome: str, summary: str, label: str) -> None:
        self._set_status(task, status)
        memory.episodic_store(task.goal, outcome, summary, task.tenant_id)
        memory.working_clear(task.id)
        self._emit(task, "stream.end", "", label, {"status": task.status.value})

    def _deny(self, task: Task, summary: str) -> None:
        task.result = "Task stopped at the human approval gate."
        self._finish(task, TaskStatus.DENIED, "denied", summary, "Task denied")

    # ---------------------------------------------------------------- approval

    def _request_approval(self, task: Task) -> None:
        task.approval_decision = None
        self._set_status(task, TaskStatus.AWAITING_APPROVAL)
        self._emit(
            task,
            "approval.required",
            "safety",
            "Human approval is required before execution",
            {"risk": task.risk.model_dump(mode="json") if task.risk else {}},
        )

    async def _wait_for_approval(self, task: Task) -> bool:
        """In-process mode: poll the store so a decision made by any API instance is seen."""
        if task.status != TaskStatus.AWAITING_APPROVAL:
            self._request_approval(task)
        deadline = time.monotonic() + config.APPROVAL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            persisted = STORE.get_task(task.id, task.tenant_id)
            if persisted is not None and persisted.approval_decision is not None:
                task.approval_decision = persisted.approval_decision
                return persisted.approval_decision
            await asyncio.sleep(0.25)
        task.approval_decision = False
        self._emit(task, "approval.timeout", "safety", "Approval window expired", {})
        return False

    async def _gate(self, task: Task) -> bool:
        """Return True when execution may proceed, False when the task has been stopped or paused."""
        if task.risk is None or not task.risk.requires_approval:
            return True
        if task.approval_decision is True:
            return True
        if task.auto_approve:
            self._emit(task, "approval.auto", "safety", "High-risk plan auto-approved by request setting", {"approved": True})
            return True
        if task.approval_decision is False:
            self._deny(task, "Human reviewer denied the plan.")
            return False
        if config.EXECUTION_BACKEND == "celery":
            # Pause durably and release the worker; approval re-enqueues the task.
            self._request_approval(task)
            return False
        if not await self._wait_for_approval(task):
            self._deny(task, "Human reviewer denied the plan or approval timed out.")
            return False
        return True

    # --------------------------------------------------------------- execution

    async def _run_step(self, task: Task, step: PlanStep) -> bool:
        agent = COMMITTEE.get(step.agent)
        if agent is None:
            step.status = StepStatus.FAILED
            step.error = f"Unknown agent: {step.agent}"
            self._emit(task, "step.failed", step.agent, step.error, {"step_id": step.id})
            return False

        step.status = StepStatus.RUNNING
        self._emit(task, "step.started", step.agent, step.title, {"step_id": step.id, "title": step.title})
        total_attempts = config.MAX_STEP_RETRIES + 1
        for attempt in range(1, total_attempts + 1):
            step.attempts += 1
            try:
                step.output = await agent.run(task, step)
                step.error = ""
                step.status = StepStatus.COMPLETED
                self._emit(
                    task,
                    "step.completed",
                    step.agent,
                    f"Step complete ({attempt} attempt{'s' if attempt != 1 else ''})",
                    {"step_id": step.id, "output": step.output, "attempts": step.attempts},
                )
                return True
            except asyncio.CancelledError:
                raise
            except llm.ProviderError as exc:
                # Configuration problem, not a flaky step: stop now and say what to fix.
                step.error = str(exc)
                step.status = StepStatus.FAILED
                self._emit(task, "provider.error", step.agent, str(exc), {"step_id": step.id, "provider": exc.provider, "detail": exc.detail, "hint": exc.hint})
                self._emit(task, "step.failed", step.agent, "Model provider unavailable", {"step_id": step.id, "error": step.error, "attempts": step.attempts})
                return False
            except Exception as exc:
                step.error = f"{type(exc).__name__}: {exc}"
                if attempt < total_attempts:
                    self._emit(task, "step.retry", step.agent, f"Attempt {attempt} failed; retrying", {"step_id": step.id, "error": step.error, "next_attempt": attempt + 1})
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                else:
                    step.status = StepStatus.FAILED
                    self._emit(task, "step.failed", step.agent, "Step failed after retries", {"step_id": step.id, "error": step.error, "attempts": step.attempts})
        return False

    async def _execute_round(self, task: Task) -> bool:
        for step in task.steps:
            if step.status == StepStatus.COMPLETED:
                continue
            if step.agent == "verifier":
                self._set_status(task, TaskStatus.VERIFYING)
            elif task.status != TaskStatus.RUNNING:
                self._set_status(task, TaskStatus.RUNNING)
            if not await self._run_step(task, step):
                return False
        return True

    def _reset_for_rework(self, task: Task) -> None:
        for step in task.steps:
            step.status = StepStatus.PENDING
            step.output = ""
            step.error = ""
        task.verified = None

    async def _plan_and_assess(self, task: Task) -> None:
        planner = COMMITTEE["planner"]
        self._set_status(task, TaskStatus.PLANNING)
        self._emit(task, "agent.started", "planner", "Decomposing goal into a task graph", {})
        task.steps = await planner.plan(task)  # type: ignore[attr-defined]
        self._emit(task, "plan.created", "planner", f"Plan ready: {len(task.steps)} steps", {"steps": [step.model_dump(mode="json") for step in task.steps]})

        self._set_status(task, TaskStatus.ASSESSING_RISK)
        self._emit(task, "agent.started", "safety", "Scoring plan risk before execution", {})
        task.risk = await COMMITTEE["safety"].assess(task, task.steps)  # type: ignore[attr-defined]
        self._emit(task, "risk.assessed", "safety", f"Risk {task.risk.score}/100 ({task.risk.level.value})", task.risk.model_dump(mode="json"))

    async def _run(self, task: Task) -> None:
        try:
            if not task.steps or task.risk is None:
                await self._plan_and_assess(task)
            else:
                # Resuming: a step that was mid-flight when the previous process stopped runs again.
                for step in task.steps:
                    if step.status in {StepStatus.RUNNING, StepStatus.FAILED}:
                        step.status = StepStatus.PENDING
                        step.error = ""

            if not await self._gate(task):
                return

            self._set_status(task, TaskStatus.RUNNING)
            for round_number in range(task.rework_count + 1, MAX_ROUNDS + 1):
                if not await self._execute_round(task):
                    raise RuntimeError(next((step.error for step in task.steps if step.status == StepStatus.FAILED), "Step execution failed"))
                if task.verified:
                    break
                if round_number < MAX_ROUNDS:
                    task.rework_count += 1
                    self._emit(task, "rework.started", "verifier", "Verification failed; starting the single allowed rework round", {"round": task.rework_count, "notes": task.verification})
                    self._reset_for_rework(task)
                    self._set_status(task, TaskStatus.RUNNING)
                else:
                    raise RuntimeError(f"Verification failed after rework: {task.verification}")

            task.result = await COMMITTEE["planner"].synthesize(task)  # type: ignore[attr-defined]
            self._emit(task, "task.completed", "planner", "Final report ready", {"result": task.result})
            self._finish(task, TaskStatus.COMPLETED, "completed", task.verification or "Completed and verified", "Task complete")
        except asyncio.CancelledError:
            # Shutdown in progress: leave the persisted state as-is so recover() can continue it.
            raise
        except Exception as exc:
            if isinstance(exc, llm.ProviderError):
                task.error = str(exc)
                self._emit(task, "provider.error", "", str(exc), {"provider": exc.provider, "detail": exc.detail, "hint": exc.hint})
            else:
                task.error = f"{type(exc).__name__}: {exc}"
            self._emit(task, "task.failed", "", task.error, {"error": task.error})
            self._finish(task, TaskStatus.FAILED, "failed", task.error, "Task failed")


ORCHESTRATOR = Orchestrator()
