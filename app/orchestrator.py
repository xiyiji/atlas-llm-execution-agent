"""Control-plane for the complete Atlas task lifecycle."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from . import audit, config, memory
from .agents import COMMITTEE
from .models import Event, PlanStep, StepStatus, Task, TaskStatus


class Orchestrator:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)
        self._approvals: dict[str, asyncio.Future[bool]] = {}
        self._runners: set[asyncio.Task] = set()

    def create_task(self, goal: str, auto_approve: bool = False) -> Task:
        task = Task(goal=goal.strip(), auto_approve=auto_approve)
        self.tasks[task.id] = task
        self._emit(task, "task.created", "", "Task accepted", {"goal": task.goal, "auto_approve": auto_approve})
        runner = asyncio.get_running_loop().create_task(self._run(task, auto_approve))
        self._runners.add(runner)
        runner.add_done_callback(self._runners.discard)
        return task

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def resolve_approval(self, task_id: str, approved: bool) -> bool:
        task = self.tasks.get(task_id)
        future = self._approvals.get(task_id)
        if not task or task.status != TaskStatus.AWAITING_APPROVAL or not future or future.done():
            return False
        future.set_result(bool(approved))
        self._emit(task, "approval.resolved", "safety", "Plan approved" if approved else "Plan denied", {"approved": approved})
        return True

    def subscribe(self, task_id: str) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers[task_id].append(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[Event]) -> None:
        subscribers = self._subscribers.get(task_id, [])
        if queue in subscribers:
            subscribers.remove(queue)
        if not subscribers:
            self._subscribers.pop(task_id, None)

    def _emit(self, task: Task, type_: str, agent: str, message: str, data: dict | None = None) -> Event:
        task.updated_at = time.time()
        event = Event(task_id=task.id, type=type_, agent=agent, message=message, data=data or {})
        audit.record(event)
        for queue in tuple(self._subscribers.get(task.id, [])):
            queue.put_nowait(event)
        return event

    def _set_status(self, task: Task, status: TaskStatus) -> None:
        task.status = status
        self._emit(task, "task.status", "", status.value, {"status": status.value})

    async def _wait_for_approval(self, task: Task) -> bool:
        future = asyncio.get_running_loop().create_future()
        self._approvals[task.id] = future
        self._set_status(task, TaskStatus.AWAITING_APPROVAL)
        self._emit(task, "approval.required", "safety", "Human approval is required before execution", {"risk": task.risk.model_dump(mode="json") if task.risk else {}})
        try:
            return await asyncio.wait_for(future, timeout=config.APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._emit(task, "approval.timeout", "safety", "Approval window expired", {})
            return False
        finally:
            self._approvals.pop(task.id, None)

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
                self._emit(task, "step.completed", step.agent, f"Step complete ({attempt} attempt{'s' if attempt != 1 else ''})", {"step_id": step.id, "output": step.output, "attempts": step.attempts})
                return True
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

    async def _run(self, task: Task, auto_approve: bool) -> None:
        try:
            self._set_status(task, TaskStatus.PLANNING)
            self._emit(task, "agent.started", "planner", "Decomposing goal into a task graph", {})
            planner = COMMITTEE["planner"]
            task.steps = await planner.plan(task)  # type: ignore[attr-defined]
            self._emit(task, "plan.created", "planner", f"Plan ready: {len(task.steps)} steps", {"steps": [step.model_dump(mode="json") for step in task.steps]})

            self._set_status(task, TaskStatus.ASSESSING_RISK)
            self._emit(task, "agent.started", "safety", "Scoring plan risk before execution", {})
            task.risk = await COMMITTEE["safety"].assess(task, task.steps)  # type: ignore[attr-defined]
            self._emit(task, "risk.assessed", "safety", f"Risk {task.risk.score}/100 ({task.risk.level.value})", task.risk.model_dump(mode="json"))

            if task.risk.requires_approval:
                if auto_approve:
                    self._emit(task, "approval.auto", "safety", "High-risk plan auto-approved by request setting", {"approved": True})
                elif not await self._wait_for_approval(task):
                    self._set_status(task, TaskStatus.DENIED)
                    task.result = "Task stopped at the human approval gate."
                    memory.episodic_store(task.goal, "denied", "Human reviewer denied the plan or approval timed out.")
                    self._emit(task, "stream.end", "", "Task denied", {"status": task.status.value})
                    return

            self._set_status(task, TaskStatus.RUNNING)
            max_rounds = 2
            for round_number in range(1, max_rounds + 1):
                if not await self._execute_round(task):
                    raise RuntimeError(next((step.error for step in task.steps if step.status == StepStatus.FAILED), "Step execution failed"))
                if task.verified:
                    break
                if round_number < max_rounds:
                    task.rework_count += 1
                    self._emit(task, "rework.started", "verifier", "Verification failed; starting the single allowed rework round", {"round": task.rework_count, "notes": task.verification})
                    for step in task.steps:
                        step.status = StepStatus.PENDING
                        step.output = ""
                        step.error = ""
                    task.verified = None
                    self._set_status(task, TaskStatus.RUNNING)
                else:
                    raise RuntimeError(f"Verification failed after rework: {task.verification}")

            task.result = await planner.synthesize(task)  # type: ignore[attr-defined]
            self._set_status(task, TaskStatus.COMPLETED)
            memory.episodic_store(task.goal, "completed", task.verification or "Completed and verified")
            self._emit(task, "task.completed", "planner", "Final report ready", {"result": task.result})
            self._emit(task, "stream.end", "", "Task complete", {"status": task.status.value})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task.error = f"{type(exc).__name__}: {exc}"
            self._set_status(task, TaskStatus.FAILED)
            memory.episodic_store(task.goal, "failed", task.error)
            self._emit(task, "task.failed", "", task.error, {"error": task.error})
            self._emit(task, "stream.end", "", "Task failed", {"status": task.status.value})


ORCHESTRATOR = Orchestrator()
