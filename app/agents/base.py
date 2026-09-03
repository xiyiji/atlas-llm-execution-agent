"""Base contract shared by all committee members."""

from __future__ import annotations

from datetime import date

from .. import llm, memory
from ..models import PlanStep, Task


class Agent:
    name = "agent"
    role = "General committee member"
    system = "You are a careful member of the Atlas execution committee."

    async def run(self, task: Task, step: PlanStep) -> str:
        prompt = (
            f"DATE: {date.today().isoformat()}\n"
            f"GOAL: {task.goal}\nSTEP: {step.title}\nDETAIL: {step.detail}\n"
            f"WORKING MEMORY:\n{memory.working_context(task.id) or '(empty)'}"
        )
        output = await llm.complete(self.system, prompt)
        memory.working_write(task.id, self.name, output)
        return output
