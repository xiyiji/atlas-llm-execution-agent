"""Verifier agent: requirement coverage and rework recommendation."""

from __future__ import annotations

from .. import llm, memory
from ..models import PlanStep, Task, VerificationDecision
from .base import Agent


class Verifier(Agent):
    name = "verifier"
    role = "Quality assurance"

    async def run(self, task: Task, step: PlanStep) -> str:
        outputs = "\n\n".join(f"{item.title}: {item.output}" for item in task.steps if item.output)
        response = await llm.complete_json(
            f"ATLAS_JSON_VERIFIER Judge whether OUTPUTS satisfy GOAL. Return only JSON like {{\"passed\": true, \"notes\": \"one or two sentences on coverage and consistency\"}}. Fail incomplete or unsafe results. {self.guard}",
            f"GOAL: {task.goal}\nOUTPUTS:\n{outputs}",
        )
        decision = VerificationDecision.model_validate(response, strict=True)
        task.verified = decision.passed
        task.verification = decision.notes
        memory.working_write(task.id, self.name, decision.notes)
        return decision.notes
