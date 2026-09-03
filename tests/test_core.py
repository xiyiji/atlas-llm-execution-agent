import asyncio

from app import llm, risk
from app.models import PlanStep, TaskStatus
from app.agents import COMMITTEE
from app.orchestrator import Orchestrator
from app.tools.code_exec import run_python


def test_json_tolerance():
    assert llm._parse_json("```json\n{\"ok\": true}\n```") == {"ok": True}
    assert llm._parse_json("prefix [1, 2] suffix") == [1, 2]


def test_risk_floor():
    assessment = risk.merge("delete production records and send email", [PlanStep(title="do it", agent="planner")], 0, [])
    assert assessment.requires_approval
    assert assessment.score >= 60


def test_sandbox_allows_math_and_blocks_io():
    allowed = asyncio.run(run_python("print(sum([1, 2, 3]))"))
    blocked = asyncio.run(run_python("import os\nprint(os.listdir('.'))"))
    assert allowed == {"ok": True, "stdout": "6\n", "stderr": ""}
    assert not blocked["ok"]


def test_demo_task_end_to_end():
    async def scenario():
        orchestrator = Orchestrator()
        task = orchestrator.create_task("Compute basic statistics for a sample time series", False)
        for _ in range(200):
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DENIED}:
                break
            await asyncio.sleep(0.01)
        assert task.status == TaskStatus.COMPLETED, task.error
        assert task.verified is True
        assert task.result

    asyncio.run(scenario())


def test_high_risk_denial():
    async def scenario():
        orchestrator = Orchestrator()
        task = orchestrator.create_task("Delete records from the production database", False)
        for _ in range(200):
            if task.status == TaskStatus.AWAITING_APPROVAL:
                break
            await asyncio.sleep(0.01)
        assert task.status == TaskStatus.AWAITING_APPROVAL
        assert orchestrator.resolve_approval(task.id, False)
        for _ in range(200):
            if task.status == TaskStatus.DENIED:
                break
            await asyncio.sleep(0.01)
        assert task.status == TaskStatus.DENIED

    asyncio.run(scenario())


def test_step_retry_recovers():
    async def scenario():
        planner = COMMITTEE["planner"]
        original = planner.run
        calls = 0

        async def flaky(task, step):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient failure")
            return await original(task, step)

        planner.run = flaky
        try:
            orchestrator = Orchestrator()
            task = orchestrator.create_task("Prepare a concise project summary", False)
            for _ in range(400):
                if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                    break
                await asyncio.sleep(0.01)
            assert task.status == TaskStatus.COMPLETED, task.error
            assert task.steps[0].attempts == 2
        finally:
            planner.run = original

    asyncio.run(scenario())


def test_verifier_triggers_only_one_rework_round():
    async def scenario():
        verifier = COMMITTEE["verifier"]
        original = verifier.run
        calls = 0

        async def fail_once(task, step):
            nonlocal calls
            calls += 1
            if calls == 1:
                task.verified = False
                task.verification = "First pass requested rework"
                return task.verification
            return await original(task, step)

        verifier.run = fail_once
        try:
            orchestrator = Orchestrator()
            task = orchestrator.create_task("Prepare a concise project summary", False)
            for _ in range(400):
                if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                    break
                await asyncio.sleep(0.01)
            assert task.status == TaskStatus.COMPLETED, task.error
            assert task.rework_count == 1
            assert calls == 2
        finally:
            verifier.run = original

    asyncio.run(scenario())
