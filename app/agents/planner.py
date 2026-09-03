"""Planner: decomposition and final synthesis."""

from __future__ import annotations

from .. import llm, memory
from ..models import PlanStep, Task
from .base import Agent


class Planner(Agent):
    name = "planner"
    role = "Planning and synthesis"
    system = "You decompose goals into safe, concrete steps and synthesize concise reports."

    async def plan(self, task: Task) -> list[PlanStep]:
        if llm.is_demo():
            goal = task.goal.lower()
            steps = [PlanStep(title=f"Clarify scope and success criteria: {task.goal}", agent="planner")]
            if any(token in goal for token in ("delete", "remove", "production", "payment", "credential", "password")):
                steps.extend([
                    PlanStep(title="Define safeguards, authorization, backup, and rollback requirements", agent="planner", detail="Produce guidance only; do not execute consequential actions."),
                    PlanStep(title="Draft a dry-run procedure and human validation checklist", agent="planner", detail="Keep all external effects behind explicit approval."),
                ])
            elif any(token in goal for token in ("research", "latest", "source", "cite", "web", "framework")):
                steps.extend([
                    PlanStep(title="Gather relevant sources across the web", agent="browser", detail="Collect concise source summaries and URLs."),
                    PlanStep(title="Cross-check facts across sources", agent="browser", detail="Compare claims and preserve citations."),
                ])
            elif any(token in goal for token in ("code", "compute", "statistics", "calculate", "python", "dataset", "time series")):
                steps.append(PlanStep(title="Implement and run the requested computation", agent="coder", detail="Use the isolated Python sandbox."))
            else:
                steps.append(PlanStep(title="Analyze the request and prepare the deliverable", agent="planner"))
            steps.append(PlanStep(title="Verify outputs against the goal", agent="verifier", detail="Check requirement coverage and consistency."))
        else:
            response = await llm.complete_json(
                "ATLAS_JSON_PLANNER Return only JSON: {\"steps\":[{\"title\":str,\"agent\":planner|coder|browser,\"detail\":str}]}. Do not include safety or verifier steps.",
                f"GOAL: {task.goal}\nRECENT EPISODIC MEMORY: {memory.episodic_recall(5)}",
                1600,
            )
            raw_steps = response.get("steps", []) if isinstance(response, dict) else []
            steps = []
            for raw in raw_steps[:8]:
                if not isinstance(raw, dict):
                    continue
                agent = str(raw.get("agent", "planner")).lower()
                if agent not in {"planner", "coder", "browser"}:
                    agent = "planner"
                title = str(raw.get("title", "")).strip()
                if title:
                    steps.append(PlanStep(title=title, agent=agent, detail=str(raw.get("detail", ""))))
            if not steps:
                steps = [PlanStep(title=f"Analyze and satisfy: {task.goal}", agent="planner")]
            steps.append(PlanStep(title="Verify outputs against the goal", agent="verifier", detail="Check requirement coverage and consistency."))
        memory.working_write(task.id, self.name, f"Created a {len(steps)}-step task graph")
        return steps

    async def synthesize(self, task: Task) -> str:
        completed = [step for step in task.steps if step.status.value == "completed" and step.agent != "verifier"]
        if llm.is_demo():
            details = "\n".join(f"- **{step.title}**: {step.output}" for step in completed)
            report = (
                f"## Result\n\nAll committee steps for **{task.goal}** completed successfully.\n\n"
                f"{details}\n\n### Verification\n\n{task.verification or 'All checks passed.'}"
            )
            if llm.last_error():
                report += f"\n\n> Live model fallback: {llm.last_error()}"
            return report
        prompt = "\n\n".join(f"{step.title}\n{step.output}" for step in task.steps)
        return await llm.complete(
            "Write a concise final Markdown report satisfying the original goal. Use only supplied step outputs; preserve source URLs.",
            f"GOAL: {task.goal}\n\nSTEP OUTPUTS:\n{prompt}\n\nVERIFICATION: {task.verification}",
            1800,
        )
