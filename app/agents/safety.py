"""Safety agent: advisory risk reasoning, merged with a deterministic floor."""

from __future__ import annotations

from .. import llm, risk
from ..models import PlanStep, RiskAssessment, Task
from .base import Agent


class Safety(Agent):
    name = "safety"
    role = "Risk assessment"

    async def assess(self, task: Task, steps: list[PlanStep]) -> RiskAssessment:
        response = await llm.complete_json(
            "ATLAS_JSON_SAFETY Return only JSON with integer score 0-100 and string-array factors.",
            f"GOAL: {task.goal}\nSTEPS: {[step.model_dump() for step in steps]}",
        )
        score = response.get("score", 0) if isinstance(response, dict) else 0
        factors = response.get("factors", []) if isinstance(response, dict) else []
        if not isinstance(factors, list):
            factors = [str(factors)]
        return risk.merge(task.goal, steps, int(score or 0), [str(item) for item in factors])
