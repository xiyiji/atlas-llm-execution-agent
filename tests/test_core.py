import asyncio

from app import config, llm, risk
from app.agents import COMMITTEE
from app.auth import issue_session, tenant_context, verify_session
from app.models import Event, PlanStep, Task, TaskStatus
from app.orchestrator import Orchestrator
from app.storage import Store
from app.tools.code_exec import run_python
from app.tools.web import _safe_public_url


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


def test_store_persists_and_isolates_tenants(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'atlas.db'}")
    task = Task(goal="persist me", tenant_id="tenant-a")
    store.save_task(task)
    event = Event(task_id=task.id, tenant_id="tenant-a", type="task.created")
    store.save_event(event)

    assert store.get_task(task.id, "tenant-a") == task
    assert store.get_task(task.id, "tenant-b") is None
    assert store.events_after(task.id, "tenant-a")[0]["id"] == event.id
    assert store.events_after(task.id, "tenant-b") == []


def test_signed_session_and_api_key_auth(monkeypatch):
    monkeypatch.setattr(config, "AUTH_REQUIRED", True)
    monkeypatch.setattr(config, "API_KEYS", "alpha:secret-a,beta:secret-b")
    monkeypatch.setattr(config, "SESSION_SECRET", "x" * 40)
    token = issue_session("alpha")
    assert verify_session(token) == "alpha"
    assert verify_session(token + "broken") is None

    context = asyncio.run(tenant_context(x_api_key="secret-b", atlas_session=None))
    assert context.tenant_id == "beta"
    session_context = asyncio.run(tenant_context(x_api_key=None, atlas_session=token))
    assert session_context.tenant_id == "alpha"


def test_ssrf_guard_rejects_local_networks():
    assert not asyncio.run(_safe_public_url("http://127.0.0.1/secret"))
    assert not asyncio.run(_safe_public_url("http://[::1]/secret"))
    assert not asyncio.run(_safe_public_url("file:///etc/passwd"))
