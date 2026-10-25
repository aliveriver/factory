"""Offline workflow acceptance using explicitly labelled test doubles."""

import asyncio
import hashlib
import json
import sys
import time

import pytest

from astrbot_plugin_research.execution import LocalExecutor
from astrbot_plugin_research.harness import make_harness
from astrbot_plugin_research.state import Store
from astrbot_plugin_research.workflow import Workflow

OWNER = '["test-platform","owner"]'
OTHER = '["test-platform","other"]'


class TestHarnessDouble:
    """TEST DOUBLE: no real harness, model, search, or remote service is used."""

    __test__ = False

    def __init__(self, scenario, config, workspace, emit):
        self.scenario = scenario
        self.config = config
        self.workspace = workspace
        self.emit = emit

    async def open(self, model, reference=None):
        self.scenario.references.append(reference)
        return {
            "reference": reference or f"TEST-SESSION-{self.config['harness']}",
            "configured": model,
            "discovered": [model],
            "verified": False,
            "capabilities": {"interactive": True, "resume": True},
        }

    async def turn(self, prompt, timeout):
        phase = prompt.split("\nPhase: ", 1)[1].split("\n", 1)[0]
        self.scenario.prompts.append((self.config["harness"], phase, prompt))
        if self.scenario.delay:
            await asyncio.sleep(self.scenario.delay)
        if phase == "research" and self.scenario.question:
            self.scenario.question = False
            output = {"question": "Which existing dataset?", "summary": "Need data"}
        elif phase == "design synthesis":
            output = {
                "plan": {
                    "baseline": "TEST baseline",
                    "change": "TEST modification",
                    "data": "TEST fixed split and seed",
                    "metric": "TEST accuracy",
                    "scale": "TEST two repetitions",
                    "decision_rule": "TEST compare differences and variation",
                    "resource_policy": "TEST local authorized only",
                    "commands": [[sys.executable, "experiment.py"]],
                    "artifacts": ["metrics.json"],
                },
                "disagreements": [],
            }
        elif phase in {"implement", "repair"}:
            output = {
                "summary": f"TEST {phase}",
                "files": {
                    "experiment.py": (
                        "from pathlib import Path\n"
                        "Path('metrics.json').write_text("
                        '\'{"baseline":0.8,"modified":0.8}\')\n'
                        "print('TEST CONTROLLED EXPERIMENT')\n"
                    )
                },
            }
        elif phase == "analyze":
            output = {
                "verdict": "no_improvement",
                "summary": "TEST no improvement is valid",
                "baseline_metrics": {"accuracy": 0.8},
                "modified_metrics": {"accuracy": 0.8},
                "limitations": ["TEST data only"],
            }
        else:
            output = {
                "summary": f"TEST {phase} {self.config['harness']}",
                "evidence": [{"source": "TEST fixture", "fact": "TEST fact"}],
                "disagreements": [],
            }
        return {"text": json.dumps(output), "usage": None}

    async def cancel(self):
        self.scenario.cancels += 1

    async def close(self):
        pass


class TestExecutorDouble:
    """TEST DOUBLE: simulates process transitions and local archive evidence."""

    __test__ = False

    def __init__(self):
        self.submissions = []
        self.failed = False
        self.unknown = False
        self.archive_failure = False
        self.running = False
        self.cancelled = []
        self.query_failures = 0

    async def preflight(self):
        return {"kind": "TEST DOUBLE"}

    async def submit(self, workspace, run_id, argv, timeout):
        self.submissions.append(run_id)
        return {"id": run_id, "directory": str(workspace / ".runs" / run_id)}

    async def query(self, reference):
        if self.query_failures:
            self.query_failures -= 1
            raise OSError("TEST disconnect")
        return {
            "id": reference["id"],
            "state": (
                "unknown"
                if self.unknown
                else "running"
                if self.running
                else "failed"
                if self.failed
                else "succeeded"
            ),
            "exit_code": 1 if self.failed else 0,
        }

    async def collect(self, workspace, reference, artifacts, limit):
        path = workspace / "metrics.json"
        path.write_text('{"baseline":0.8,"modified":0.8}', encoding="utf-8")
        return [
            {
                "path": "metrics.json",
                "status": "not_saved" if self.archive_failure else "downloaded",
                "local": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        ]

    async def cancel(self, reference):
        self.cancelled.append(reference["id"])


class Scenario:
    """Arrange test-only dependencies; production never selects these classes."""

    def __init__(self, root):
        self.prompts = []
        self.references = []
        self.cancels = 0
        self.question = False
        self.delay = 0
        self.executor = TestExecutorDouble()
        self.messages = []
        self.store = Store(root)
        self.settings = {
            "participants": {
                "builder": {"harness": "codex", "model": "TEST-CODEX"},
                "reviewer": {"harness": "deepseek", "model": "TEST-DEEPSEEK"},
            },
            "assignments": {
                "research": ["reviewer"],
                "design": ["builder", "reviewer"],
                "implement": ["builder"],
                "analyze": ["reviewer"],
                "summary": ["builder"],
            },
            "platform": "local",
            "platforms": {"local": {"type": "local", "allow_execute": True}},
            "limits": {"repair_rounds": 1, "discussion_rounds": 2},
        }
        self.flow = self.new_flow()

    def new_flow(self):
        return Workflow(
            self.store,
            self.settings,
            self.notify,
            harness_factory=lambda c, w, e: TestHarnessDouble(self, c, w, e),
            executor_factory=lambda _: self.executor,
        )

    async def notify(self, task, text):
        self.messages.append(text)

    async def idle(self, task_id):
        worker = self.flow.workers.get(task_id)
        if worker:
            await asyncio.wait_for(asyncio.shield(worker), 8)
        await asyncio.sleep(0)
        return self.store.load(task_id, OWNER)

    async def plan(self):
        task = self.flow.create(OWNER, "TEST-address", "TEST compare a modification")
        self.flow.schedule(task["id"])
        return await self.idle(task["id"])


@pytest.mark.asyncio
async def test_complete_flow_constraints_two_harnesses_and_no_improvement(tmp_path):
    scenario = Scenario(tmp_path / "tasks")
    agents = tmp_path / "USER_AGENTS.md"
    original = b"# User constraints\r\nOnly write inside the workspace.\r\n"
    agents.write_bytes(original)
    scenario.settings["agents_file"] = str(agents)
    task = await scenario.plan()
    assert task["status"] == "waiting_confirmation"
    assert not scenario.executor.submissions
    assert task["approved_version"] is None
    discussion = [p for p in scenario.prompts if p[1] == "design"]
    assert [p[0] for p in discussion] == ["codex", "deepseek", "codex", "deepseek"]
    assert all(original.decode() in p[2] for p in scenario.prompts)
    assert agents.read_bytes() == original
    assert "plan" not in agents.read_text()
    with pytest.raises(ValueError, match="current"):
        await scenario.flow.interact(task["id"], OWNER, "approve", version=99)
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    done = await scenario.idle(task["id"])
    assert done["status"] == "completed"
    assert done["result"]["verdict"] == "no_improvement"
    assert len(scenario.executor.submissions) == 1
    assert done["usage"]["repair_rounds"] == 0
    assert done["usage"]["unknown_calls"] == done["usage"]["model_calls"]
    assert (scenario.store.directory(task["id"]) / "experiment.md").exists()
    assert list((scenario.store.directory(task["id"]) / "transcripts").glob("*.json"))
    assert done["notifications"][-1]["delivered"]


@pytest.mark.asyncio
async def test_question_reply_rejection_and_owner_isolation(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.question = True
    task = await scenario.plan()
    assert task["status"] == "waiting_user"
    with pytest.raises(PermissionError):
        await scenario.flow.interact(task["id"], OTHER, "reply", "steal task")
    with pytest.raises(ValueError, match="reply"):
        await scenario.flow.interact(task["id"], OWNER, "continue")
    await scenario.flow.interact(task["id"], OWNER, "reply", "Use the existing split")
    task = await scenario.idle(task["id"])
    assert task["status"] == "waiting_confirmation"
    await scenario.flow.interact(task["id"], OWNER, "reject", version=1)
    assert scenario.store.load(task["id"])["status"] == "rejected"
    assert not scenario.executor.submissions
    assert scenario.store.list(OTHER) == []


@pytest.mark.asyncio
async def test_repair_limit_is_observable_and_bounded(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.executor.failed = True
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    task = await scenario.idle(task["id"])
    assert task["status"] == "paused"
    assert "repair round limit" in task["reason"]
    assert task["usage"]["repair_rounds"] == 1
    assert len(scenario.executor.submissions) == 2
    assert len([p for p in scenario.prompts if p[1] == "repair"]) == 1


@pytest.mark.asyncio
async def test_model_limit_and_cancellation(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.settings["limits"]["model_calls"] = 1
    task = await scenario.plan()
    assert task["status"] == "paused"
    assert "Model call limit" in task["reason"]
    scenario.settings["limits"]["model_calls"] = 30
    scenario.delay = 10
    task2 = scenario.flow.create(OWNER, "TEST-address", "TEST cancellation")
    scenario.flow.schedule(task2["id"])
    await asyncio.sleep(0.05)
    await scenario.flow.interact(task2["id"], OWNER, "stop")
    assert scenario.store.load(task2["id"])["status"] == "cancelled"
    assert scenario.cancels >= 1


@pytest.mark.asyncio
async def test_disconnect_and_restart_never_resubmit_existing_run(tmp_path):
    scenario = Scenario(tmp_path)
    task = await scenario.plan()
    scenario.executor.unknown = True
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    task = await scenario.idle(task["id"])
    assert task["status"] == "paused"
    assert "ambiguous" in task["reason"]
    assert len(scenario.executor.submissions) == 1
    await scenario.flow.close()
    scenario.flow = scenario.new_flow()
    await scenario.flow.recover()
    scenario.executor.unknown = False
    scenario.executor.query_failures = 1
    await scenario.flow.interact(task["id"], OWNER, "continue")
    task = await scenario.idle(task["id"])
    assert task["status"] == "completed"
    assert len(scenario.executor.submissions) == 1


@pytest.mark.asyncio
async def test_cancel_records_process_independently(tmp_path):
    scenario = Scenario(tmp_path)
    task = await scenario.plan()
    scenario.executor.running = True
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    for _ in range(100):
        if scenario.executor.submissions:
            break
        await asyncio.sleep(0.01)
    await scenario.flow.interact(task["id"], OWNER, "stop")
    task = scenario.store.load(task["id"])
    assert task["status"] == "cancelled"
    assert task["runs"][-1]["state"] == "cancel_requested"
    assert scenario.executor.cancelled == scenario.executor.submissions


@pytest.mark.asyncio
async def test_archive_failure_and_notification_fallback(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.executor.archive_failure = True

    async def unavailable(task, text):
        raise OSError("TEST platform cannot send")

    scenario.flow.notify = unavailable
    task = await scenario.plan()
    assert not task["notifications"][-1]["delivered"]
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    task = await scenario.idle(task["id"])
    assert task["status"] == "paused"
    assert "archive" in task["reason"]
    assert task["manifest"][0]["status"] == "not_saved"
    before = task["usage"]["model_calls"]
    scenario.executor.archive_failure = False
    await scenario.flow.interact(task["id"], OWNER, "continue")
    done = await scenario.idle(task["id"])
    assert done["status"] == "completed"
    assert done["usage"]["model_calls"] == before
    assert len(scenario.executor.submissions) == 1


@pytest.mark.asyncio
async def test_recovery_supervises_process_without_resuming_model_work(tmp_path):
    scenario = Scenario(tmp_path)
    task = await scenario.plan()
    scenario.executor.running = True
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    for _ in range(100):
        if scenario.executor.submissions:
            break
        await asyncio.sleep(0.01)
    await scenario.flow.close()
    before = scenario.store.load(task["id"])["usage"]["model_calls"]
    scenario.executor.running = False
    scenario.flow = scenario.new_flow()
    await scenario.flow.recover()
    await asyncio.wait_for(scenario.flow.monitors[task["id"]], 5)
    task = scenario.store.load(task["id"])
    assert task["status"] == "paused"
    assert task["runs"][-1]["state"] == "succeeded"
    assert task["manifest"][0]["status"] == "downloaded"
    assert task["usage"]["model_calls"] == before
    assert len(scenario.executor.submissions) == 1
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_autodl_unknown_stop_is_queried_after_restart_not_repeated(
    tmp_path, monkeypatch
):
    from astrbot_plugin_research import workflow as module

    operations = []

    class AutoDLManagerDouble:
        """TEST DOUBLE: lifecycle intent verification without AutoDL credentials."""

        def __init__(self, config):
            pass

        async def invoke(self, operation, instance=None):
            operations.append(operation)
            return {"status": "running"}

        async def finish(self, archive_ok):
            operations.append("stop")
            raise OSError("TEST ambiguous stop result")

    monkeypatch.setattr(module, "AutoDL", AutoDLManagerDouble)
    scenario = Scenario(tmp_path)
    scenario.settings["platform"] = "cloud"
    scenario.settings["platforms"]["cloud"] = {
        "type": "autodl",
        "ssh": {"root": "/TEST/remote"},
        "autodl": {"instance": "TEST-instance", "stop_policy": "always"},
    }
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    task = await scenario.idle(task["id"])
    assert task["status"] == "completed"
    assert task["instance_stop"] == "submitting"
    assert task["instance_state"]["status"] == "unknown"
    assert operations.count("stop") == 1
    await scenario.flow.close()
    scenario.flow = scenario.new_flow()
    await scenario.flow.recover()
    assert operations[-1] == "status"
    assert operations.count("stop") == 1
    assert len(scenario.executor.submissions) == 1
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_real_local_execution_with_test_harness_is_labelled_controlled(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.flow.executor_factory = LocalExecutor
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    task = await scenario.idle(task["id"])
    assert task["status"] == "completed", task["reason"]
    assert task["runs"][-1]["state"] == "succeeded"
    assert all(entry["sha256"] for entry in task["manifest"])
    assert (
        "TEST CONTROLLED EXPERIMENT"
        in task["evidence"][f".runs/{task['runs'][0]['id']}/stdout.log"]
    )


def test_storage_cas_invalid_paths_and_forbidden_agents_write(tmp_path):
    scenario = Scenario(tmp_path)
    task = scenario.flow.create(OWNER, "TEST-address", "TEST idea")
    old = scenario.store.load(task["id"])
    task["reason"] = "changed"
    scenario.store.save(task)
    with pytest.raises(RuntimeError, match="concurrently"):
        scenario.store.save(old)
    for path in [
        "../outside",
        "AGENTS.md",
        "nested/AGENTS.md",
        ".runs/data",
        "C:/outside",
    ]:
        with pytest.raises(ValueError):
            scenario.flow.write_files(task, {path: "TEST"})
    with pytest.raises(SyntaxError):
        scenario.flow.write_files(task, {"valid.py": "x = 1", "invalid.py": "if ("})
    assert not (
        scenario.store.directory(task["id"]) / "workspace" / "valid.py"
    ).exists()
    with pytest.raises(ValueError):
        scenario.store.directory("../../escape")
    with pytest.raises(ValueError):
        make_harness({"harness": "TEST-fake"}, tmp_path, None)


@pytest.mark.asyncio
async def test_crash_recovery_does_not_reset_active_time_budget(tmp_path):
    scenario = Scenario(tmp_path)
    task = scenario.flow.create(OWNER, "TEST-address", "TEST idea")
    task["status"] = "active"
    task["usage"]["active_seconds"] = 2
    task["limits"]["wall_seconds"] = 5
    task["active_started_at"] = time.time() - 4
    scenario.store.save(task)
    await scenario.flow.recover()
    recovered = scenario.store.load(task["id"])
    assert recovered["usage"]["active_seconds"] >= 6
    assert recovered["usage"]["recovery_wall_seconds"] >= 4
    assert recovered["active_started_at"] is None
    await scenario.flow.interact(task["id"], OWNER, "continue")
    paused = await scenario.idle(task["id"])
    assert "active-time limit" in paused["reason"]
    assert paused["usage"]["model_calls"] == 0
