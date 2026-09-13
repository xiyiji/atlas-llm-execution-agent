"""Coder agent backed by the isolated Python execution tool."""

from __future__ import annotations

from .. import llm, memory
from ..models import CodeProposal, PlanStep, Task
from ..tools.code_exec import run_python
from .base import Agent


class Coder(Agent):
    name = "coder"
    role = "Sandboxed code execution"

    async def run(self, task: Task, step: PlanStep) -> str:
        response = await llm.complete_json(
            "ATLAS_JSON_CODE Return only JSON {\"code\": \"safe Python source\"}. Use stdlib only; print the result.",
            f"GOAL: {task.goal}\nSTEP: {step.title}\nDETAIL: {step.detail}\n"
            f"VERIFIER FEEDBACK: {task.verification or '(first pass)'}\n"
            f"WORKING MEMORY:\n{memory.working_context(task.id) or '(empty)'}\n"
            f"PREVIOUS ERROR: {step.error or '(none)'}",
        )
        code = CodeProposal.model_validate(response, strict=True).code
        result = await run_python(code)
        output = f"Code:\n```python\n{code}\n```\n\n"
        output += f"Output:\n```\n{result['stdout'].strip() or result['stderr'].strip()}\n```"
        memory.working_write(task.id, self.name, output)
        if not result["ok"]:
            raise RuntimeError(result["stderr"] or "sandbox execution failed")
        return output
