"""Safety agent: advisory risk reasoning, merged with a deterministic floor."""

from __future__ import annotations

from .. import llm, risk
from ..models import PlanStep, RiskAssessment, SafetyDecision, Task
from .base import Agent


class Safety(Agent):
    name = "safety"
    role = "Risk assessment"

    async def assess(self, task: Task, steps: list[PlanStep]) -> RiskAssessment:
        response = await llm.complete_json(
            "ATLAS_JSON_SAFETY Return only JSON with integer score 0-100 and string-array factors.",
            f"GOAL: {task.goal}\nSTEPS: {[step.model_dump() for step in steps]}",
        )
        decision = SafetyDecision.model_validate(response, strict=True)
        return risk.merge(task.goal, steps, decision.score, decision.factors)
