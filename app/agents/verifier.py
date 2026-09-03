"""Verifier agent: requirement coverage and rework recommendation."""

from __future__ import annotations

from .. import llm, memory
from ..models import PlanStep, Task
from .base import Agent


class Verifier(Agent):
    name = "verifier"
    role = "Quality assurance"

    async def run(self, task: Task, step: PlanStep) -> str:
        outputs = "\n\n".join(f"{item.title}: {item.output}" for item in task.steps if item.output)
        response = await llm.complete_json(
            f"ATLAS_JSON_VERIFIER Return only JSON {{\"passed\":bool,\"notes\":str}}. Fail incomplete or unsafe results. {self.guard}",
            f"GOAL: {task.goal}\nOUTPUTS:\n{outputs}",
        )
        passed = bool(response.get("passed", False)) if isinstance(response, dict) else False
        notes = str(response.get("notes", "Verifier returned no notes")) if isinstance(response, dict) else "Verifier returned invalid output"
        task.verified = passed
        task.verification = notes
        memory.working_write(task.id, self.name, notes)
        return notes
