"""Controlled routing and unlimited-policy tests; no live harness or cloud."""

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

from astrbot_plugin_research.execution import LocalExecutor, SSHExecutor, make_executor
from astrbot_plugin_research.state import DEFAULT_LIMITS
from astrbot_plugin_research.tests.test_allocation import (
    ResourceHarnessDouble,
    configured_scenario,
)
from astrbot_plugin_research.tests.test_execution import SSHTransportDouble
from astrbot_plugin_research.tests.test_workflow import OWNER, Scenario


class PolicyHarnessDouble(ResourceHarnessDouble):
    """TEST DOUBLE: explicit platform judgments and controlled completion signals."""

    async def turn(self, prompt, timeout):
        self.scenario.timeouts.append(timeout)
        phase, body = prompt.split("\nPhase: ", 1)[1].split("\n", 1)
        payload = json.loads(body.split("\n\n", 1)[0])
        response = await super().turn(prompt, timeout)
        output = json.loads(response["text"])
        if phase == "platform selection":
            output = {
                "assessments": [
                    {
                        "platform": candidate["name"],
                        "suitability": self.scenario.suitability.get(
                            candidate["name"],
                            "suitable" if candidate["configured"] else "unsuitable",
                        ),
                        "reason": f"TEST requirement and capability assessment for {candidate['name']}",
                    }
                    for candidate in payload["context"]["candidates"]
                ]
            }
            if self.scenario.invalid_assessments:
                output["assessments"] = self.scenario.invalid_assessments
        if phase == "design":
            output["discussion_complete"] = payload["context"]["round"] >= 3
        if self.scenario.syntax_repairs and phase in {"implement", "repair"}:
            if phase == "repair":
                self.scenario.syntax_repairs_seen += 1
            if self.scenario.syntax_repairs_seen < self.scenario.syntax_repairs:
                output["files"] = {"experiment.py": "if ("}
        response["text"] = json.dumps(output)
        return response


@pytest.fixture
def routed(tmp_path):
    scenario = Scenario(tmp_path / "tasks")
    scenario.timeouts = []
    scenario.suitability = {}
    scenario.invalid_assessments = None
    scenario.syntax_repairs = 0
    scenario.syntax_repairs_seen = 0
    scenario.settings["platform"] = "auto"
    scenario.settings["platform_order"] = ["local", "ssh"]
    scenario.settings["platforms"]["ssh"] = {
        "type": "ssh",
        "capabilities": "TEST GPU server with preinstalled dependencies",
        "ssh": {
            "host": "192.0.2.10",
            "user": "test",
            "root": "/TEST/tasks",
            "allow_execute": True,
            "password": "TEST-PRIVATE-NOT-FOR-MODELS",
        },
    }
    scenario.flow.harness_factory = lambda c, w, e: PolicyHarnessDouble(
        scenario, c, w, e
    )
    return scenario


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order", "judgments", "chosen"),
    [
        (["local", "ssh"], {}, "local"),
        (["ssh", "local"], {}, "ssh"),
        (["local", "ssh"], {"local": "unsuitable"}, "ssh"),
    ],
)
async def test_first_suitable_platform_is_selected_before_execution(
    routed, order, judgments, chosen
):
    routed.settings["platform_order"] = order
    routed.suitability = judgments
    task = await routed.plan()
    assert task["status"] == "waiting_confirmation", task["reason"]
    assert task["platform"] == chosen
    assert task["plan"]["execution_platform"]["name"] == chosen
    assert task["platform_selection"]["chosen"] == chosen
    assert not routed.executor.submissions
    selection_prompt = next(
        p[2] for p in routed.prompts if p[1] == "platform selection"
    )
    assert "memory_total_bytes" in selection_prompt
    assert "TEST-PRIVATE-NOT-FOR-MODELS" not in selection_prompt
    assert all("harness_location" in p[2] for p in routed.prompts)
    await routed.flow.interact(task["id"], OWNER, "approve", version=1)
    done = await routed.idle(task["id"])
    assert done["status"] == "completed", done["reason"]
    assert len(routed.executor.submissions) == 1


@pytest.mark.asyncio
async def test_unknown_earlier_platform_waits_instead_of_escalating(routed):
    routed.suitability["local"] = "unknown"
    task = await routed.plan()
    assert task["status"] == "waiting_user"
    assert task["phase"] == "select_platform"
    assert task["platform"] == "auto"
    assert not task["approved_version"]
    assert not routed.executor.submissions
    routed.suitability["local"] = "unsuitable"
    await routed.flow.interact(task["id"], OWNER, "reply", "TEST local GPU is absent")
    revised = await routed.idle(task["id"])
    assert revised["status"] == "waiting_confirmation"
    assert revised["platform"] == "ssh"


@pytest.mark.asyncio
async def test_all_unsuitable_platforms_wait_for_resources(routed):
    routed.suitability = {"local": "unsuitable", "ssh": "unsuitable"}
    task = await routed.plan()
    assert task["status"] == "waiting_user"
    assert "资源" in task["pending"]["question"]
    assert not routed.executor.submissions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assessments",
    [
        [{"platform": "unapproved", "suitability": "suitable", "reason": "TEST"}],
        [
            {"platform": "ssh", "suitability": "suitable", "reason": "TEST"},
            {"platform": "local", "suitability": "unsuitable", "reason": "TEST"},
        ],
    ],
)
async def test_malformed_assessment_cannot_choose_a_destination(routed, assessments):
    routed.invalid_assessments = assessments
    task = await routed.plan()
    assert task["status"] == "paused"
    assert "priority order" in task["reason"]
    assert not routed.executor.submissions


@pytest.mark.asyncio
async def test_protected_server_cannot_be_selected_or_probed(routed):
    routed.settings["platforms"]["ssh"]["ssh"]["host"] = "10.8.176.151"
    routed.suitability = {"local": "unsuitable", "ssh": "suitable"}
    constructed = []
    routed.flow.executor_factory = lambda config: constructed.append(config)
    task = await routed.plan()
    assert task["status"] == "paused"
    assert "protected" in task["reason"]
    assert constructed == []


@pytest.mark.asyncio
async def test_per_platform_agents_apply_to_every_local_harness_and_bind_approval(
    routed, tmp_path
):
    common = tmp_path / "common.md"
    common.write_text("TEST-COMMON-RULES", encoding="utf-8")
    routed.settings["agents_file"] = str(common)
    for name in ("local", "ssh"):
        path = tmp_path / f"{name}.md"
        path.write_text(f"TEST-{name.upper()}-ONLY-RULES", encoding="utf-8")
        routed.settings["platforms"][name]["agents_file"] = str(path)
    routed.suitability["local"] = "unsuitable"
    task = await routed.plan()
    assert task["platform"] == "ssh"
    relevant = [p for p in routed.prompts if p[1] in {"design", "design synthesis"}]
    assert {p[0] for p in relevant} == {"codex", "deepseek"}
    assert all(
        "TEST-COMMON-RULES" in p[2] and "TEST-SSH-ONLY-RULES" in p[2] for p in relevant
    )
    assert all("TEST-LOCAL-ONLY-RULES" not in p[2] for p in relevant)
    assert task["plan"]["execution_platform"]["agents"] == task["agents"]
    original = (tmp_path / "ssh.md").read_bytes()
    await routed.flow.interact(task["id"], OWNER, "approve", version=1)
    done = await routed.idle(task["id"])
    assert done["status"] == "completed", done["reason"]
    assert (tmp_path / "ssh.md").read_bytes() == original
    assert all("Never install or start any harness" in p[2] for p in routed.prompts)


@pytest.mark.asyncio
async def test_changed_platform_rules_require_new_plan_before_any_executor(
    routed, tmp_path
):
    rules = tmp_path / "local.md"
    rules.write_text("TEST original rules", encoding="utf-8")
    routed.settings["platforms"]["local"]["agents_file"] = str(rules)
    task = await routed.plan()
    rules.write_text("TEST changed rules", encoding="utf-8")
    constructed = []
    routed.flow.executor_factory = lambda config: constructed.append(config)
    await routed.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await routed.idle(task["id"])
    assert paused["status"] == "paused"
    assert "constraints changed" in paused["reason"]
    assert constructed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "remove"])
async def test_changed_rule_path_invalidates_approval(routed, tmp_path, change):
    first = tmp_path / "first.md"
    first.write_text("TEST unchanged content", encoding="utf-8")
    routed.settings["platforms"]["local"]["agents_file"] = str(first)
    task = await routed.plan()
    if change == "replace":
        second = tmp_path / "second.md"
        second.write_bytes(first.read_bytes())
        routed.settings["platforms"]["local"]["agents_file"] = str(second)
    else:
        routed.settings["platforms"]["local"].pop("agents_file")
    constructed = []
    routed.flow.executor_factory = lambda config: constructed.append(config)
    await routed.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await routed.idle(task["id"])
    assert paused["status"] == "paused"
    assert "constraints changed" in paused["reason"]
    assert constructed == []


@pytest.mark.asyncio
async def test_supplement_reselects_unallocated_task_and_requires_new_approval(routed):
    task = await routed.plan()
    assert task["platform"] == "local"
    routed.suitability["local"] = "unsuitable"
    await routed.flow.interact(
        task["id"], OWNER, "supplement", "TEST new requirements need the server GPU"
    )
    revised = await routed.idle(task["id"])
    assert revised["platform"] == "ssh"
    assert revised["status"] == "waiting_confirmation"
    assert revised["approved_version"] is None
    assert revised["plan_version"] > task["plan_version"]
    assert not routed.executor.submissions
    with pytest.raises(ValueError, match="current"):
        await routed.flow.interact(task["id"], OWNER, "approve", version=1)
    relevant = [p for p in routed.prompts if p[1] == "design"]
    payload = json.loads(relevant[-1][2].split("\nPhase: design\n")[1].split("\n\n")[0])
    assert payload["execution_environment"]["python"] == "python3"
    assert payload["execution_environment"]["harness_location"] == "local"


@pytest.mark.asyncio
async def test_unresolved_original_run_cannot_be_replaced_by_supplement(routed):
    task = await routed.plan()
    routed.executor.unknown = True
    try:
        await routed.flow.interact(task["id"], OWNER, "approve", version=1)
        paused = await routed.idle(task["id"])
        assert paused["status"] == "paused"
        assert paused["runs"][-1]["state"] == "unknown"
        routed.settings["platform_order"] = ["ssh", "local"]
        with pytest.raises(ValueError, match="Original run is unresolved"):
            await routed.flow.interact(
                task["id"], OWNER, "supplement", "TEST request different resources"
            )
        assert routed.store.load(task["id"]) == paused
        assert len(routed.executor.submissions) == 1
    finally:
        await routed.flow.close()


@pytest.mark.asyncio
async def test_unlimited_waiting_model_turn_is_cancellable(routed):
    routed.settings["limits"] = dict.fromkeys(DEFAULT_LIMITS)
    routed.delay = 30
    task = routed.flow.create(OWNER, "TEST-address", "TEST cancel unlimited turn")
    routed.flow.schedule(task["id"])
    for _ in range(100):
        if routed.prompts:
            break
        await asyncio.sleep(0.01)
    assert routed.prompts
    stopped = await asyncio.wait_for(routed.flow.interact(task["id"], OWNER, "stop"), 3)
    assert stopped["status"] == "cancelled"
    assert task["id"] not in routed.flow.workers
    assert routed.cancels
    assert not routed.executor.submissions


@pytest.mark.asyncio
async def test_missing_execution_permission_does_not_escalate_to_server(routed):
    routed.settings["platforms"]["local"]["allow_execute"] = False
    routed.flow.executor_factory = make_executor
    task = await routed.plan()
    assert task["platform"] == "local"
    await routed.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await routed.idle(task["id"])
    assert paused["status"] == "paused"
    assert "not been authorized" in paused["reason"]
    assert paused["runs"] == []


@pytest.mark.asyncio
async def test_auto_cloud_choice_keeps_approved_allocation_and_shutdown(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path / "cloud", monkeypatch)
    scenario.timeouts, scenario.suitability = [], {"local": "unsuitable"}
    scenario.invalid_assessments = None
    scenario.syntax_repairs = scenario.syntax_repairs_seen = 0
    scenario.settings["platform"] = "auto"
    scenario.settings["platform_order"] = ["local", "cloud"]
    scenario.flow.harness_factory = lambda c, w, e: PolicyHarnessDouble(
        scenario, c, w, e
    )
    task = await scenario.plan()
    assert task["platform"] == "cloud"
    assert task["plan"]["resources"]["profile"] == "small"
    assert not service.calls
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    done = await scenario.idle(task["id"])
    assert done["status"] == "completed", done["reason"]
    assert done["allocation"]["stopped"]
    assert [call[2] for call in service.calls].count("create") == 1
    assert [call[2] for call in service.calls].count("stop") == 1


@pytest.mark.asyncio
async def test_all_unlimited_limits_converge_repair_and_finish(routed):
    routed.settings["platform"] = "local"
    routed.settings["limits"] = dict.fromkeys(DEFAULT_LIMITS)
    routed.syntax_repairs = 6
    task = routed.flow.create(OWNER, "TEST-address", "TEST unlimited policies")
    task["usage"]["model_calls"] = 100_000
    task["usage"]["active_seconds"] = 100_000
    routed.store.save(task)
    routed.flow.schedule(task["id"])
    task = await routed.idle(task["id"])
    assert task["status"] == "waiting_confirmation", task["reason"]
    discussion = [p for p in routed.prompts if p[1] == "design"]
    assert len(discussion) == 6
    for _, _, prompt in discussion:
        payload = json.loads(prompt.split("\nPhase: design\n")[1].split("\n\n")[0])
        assert payload["context"]["round_limit"] is None
    await routed.flow.interact(task["id"], OWNER, "approve", version=1)
    done = await routed.idle(task["id"])
    assert done["status"] == "completed", done["reason"]
    assert done["usage"]["repair_rounds"] == 6
    assert all(timeout is None for timeout in routed.timeouts)


@pytest.mark.asyncio
async def test_unlimited_network_retries_query_original_run_only(routed):
    routed.settings["platform"] = "local"
    routed.settings["limits"]["network_retries"] = None
    task = await routed.plan()
    routed.executor.query_failures = 3
    await routed.flow.interact(task["id"], OWNER, "approve", version=1)
    done = await routed.idle(task["id"])
    assert done["status"] == "completed", done["reason"]
    assert len(routed.executor.submissions) == 1


@pytest.mark.asyncio
async def test_real_controlled_local_runner_unlimited_duration_is_cancellable(tmp_path):
    executor = LocalExecutor({"allow_execute": True})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reference = await executor.submit(
        workspace,
        uuid.uuid4().hex,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        None,
    )
    try:
        for _ in range(100):
            state = await executor.query(reference)
            if state["state"] == "running":
                break
            await asyncio.sleep(0.05)
        assert state["state"] == "running"
        assert (
            json.loads((Path(reference["directory"]) / "request.json").read_text())[
                "timeout"
            ]
            is None
        )
    finally:
        await executor.cancel(reference)
        for _ in range(100):
            state = await executor.query(reference)
            if state["state"] == "cancelled":
                break
            await asyncio.sleep(0.05)
    assert state["state"] == "cancelled"
    limited = await executor.collect(workspace, reference, [], 1)
    assert any(item["status"] == "not_saved" for item in limited)
    unlimited = await executor.collect(workspace, reference, [], None)
    assert all(item["status"] == "downloaded" for item in unlimited)


@pytest.mark.asyncio
async def test_offline_ssh_unlimited_deadline_archive_and_local_harness_boundary(
    tmp_path,
):
    transport = SSHTransportDouble()
    executor = SSHExecutor(
        {
            "host": "192.0.2.10",
            "user": "test",
            "root": "/TEST/tasks",
            "allow_execute": True,
        },
        transport,
    )
    (tmp_path / "experiment.py").write_text("print('TEST')", encoding="utf-8")
    reference = await executor.submit(
        tmp_path, uuid.uuid4().hex, ["python3", "experiment.py"], None
    )
    request = json.loads(
        (tmp_path / ".runs" / reference["id"] / "request.json").read_text()
    )
    assert request["timeout"] is None
    entries = await executor.collect(tmp_path, reference, ["metrics.json"], None)
    assert all(item["status"] == "downloaded" for item in entries)
    commands = "\n".join(" ".join(call) for call in transport.calls)
    assert "codex" not in commands and "dsh" not in commands
    assert "pip install" not in commands and "npm install" not in commands
