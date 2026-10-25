"""Offline allocation tests; every AutoDL and SSH interaction is a test double."""

import asyncio
import json
from copy import deepcopy

import pytest

from astrbot_plugin_research import allocation as allocation_module
from astrbot_plugin_research.allocation import Allocation, resource_spec
from astrbot_plugin_research.execution import AutoDL
from astrbot_plugin_research.tests.test_workflow import (
    OWNER,
    Scenario,
    TestHarnessDouble,
)


class ResourceHarnessDouble(TestHarnessDouble):
    """TEST DOUBLE: choose a resource profile without invoking a real model."""

    async def turn(self, prompt, timeout):
        response = await super().turn(prompt, timeout)
        output = json.loads(response["text"])
        if "plan" in output:
            output["plan"]["resources"] = {
                "profile": "small",
                "reason": "TEST minimal comparison fits the smaller approved profile",
            }
        response["text"] = json.dumps(output)
        return response


class AutoDLServiceDouble:
    """TEST DOUBLE: simulate accepted, lost, and delayed API responses offline."""

    def __init__(self):
        self.calls = []
        self.instance = None
        self.name = None
        self.state = "running"
        self.create_error = False
        self.hidden = False
        self.stop_error = False
        self.stop_pending = False
        self.create_entered = asyncio.Event()
        self.create_wait = None
        self.snapshot_error = False
        self.endpoint = "TEST.autodl.invalid"

    async def transport(self, argv, **kwargs):
        self.calls.append(argv)
        operation = argv[2]
        data = None
        if operation == "create":
            self.instance = "TEST-created-instance"
            self.name = argv[argv.index("--name") + 1]
            self.create_entered.set()
            if self.create_wait:
                await self.create_wait.wait()
            if self.create_error:
                raise OSError("TEST create was accepted but its response was lost")
            data = self.instance
        elif operation == "list":
            data = {
                "list": (
                    [{"uuid": self.instance, "name": self.name}]
                    if self.instance and not self.hidden
                    else []
                ),
                "max_page": 1,
            }
        elif operation == "status":
            data = self.state
        elif operation == "get":
            if self.snapshot_error:
                raise OSError("TEST snapshot failed")
            data = {
                "proxy_host": self.endpoint,
                "ssh_port": 22222,
                "root_password": "TEST-PRIVATE-PASSWORD",
                "jupyter_token": "TEST-PRIVATE-TOKEN",
            }
        elif operation == "stop":
            if self.stop_error:
                raise OSError("TEST shutdown remains unconfirmed")
            if not self.stop_pending:
                self.state = "shutdown"
        elif operation == "start":
            self.state = "running"
        else:
            raise AssertionError(f"Unexpected test operation: {operation}")
        return json.dumps({"code": "Success", "data": data}).encode()


def configured_scenario(tmp_path, monkeypatch):
    """Build an offline workflow with approved profiles and injected transports.

    Args:
        tmp_path: Isolated test directory.
        monkeypatch: Pytest patch scope.

    Returns:
        Scenario and injected AutoDL service double.
    """
    scenario = Scenario(tmp_path)
    service = AutoDLServiceDouble()
    monkeypatch.setattr(
        allocation_module, "AutoDL", lambda config: AutoDL(config, service.transport)
    )
    scenario.flow.harness_factory = lambda c, w, e: ResourceHarnessDouble(
        scenario, c, w, e
    )
    scenario.settings["platform"] = "cloud"
    scenario.settings["platforms"]["cloud"] = {
        "type": "autodl",
        "ssh": {
            "user": "root",
            "root": "/TEST/dedicated-experiments",
            "allow_execute": True,
        },
        "autodl": {
            "mode": "create",
            "allow_create": True,
            "allow_discovery": True,
            "allow_stop": True,
            "allow_start": True,
            "resource_profiles": {
                "small": {
                    "description": "TEST approved small GPU image with SSH keys",
                    "gpu_spec": "TEST-gpu",
                    "image": "TEST-image",
                    "cuda_min": 118,
                    "gpus": 1,
                },
                "large": {
                    "gpu_spec": "TEST-large-gpu",
                    "image": "TEST-image",
                    "cuda_min": 118,
                    "gpus": 2,
                },
            },
        },
    }
    return scenario, service


@pytest.mark.asyncio
async def test_approved_resource_choice_create_execute_archive_and_confirm_shutdown(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    task = await scenario.plan()
    assert not service.calls
    assert task["status"] == "waiting_confirmation"
    assert task["plan"]["resources"]["spec"]["gpus"] == 1
    assert task["plan"]["resources"]["spec"]["image"] == "TEST-image"
    assert "allocation_profiles" in scenario.prompts[0][2]
    platforms = []
    scenario.flow.executor_factory = lambda platform: (
        platforms.append(deepcopy(platform)) or scenario.executor
    )
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    done = await scenario.idle(task["id"])
    assert done["status"] == "completed", done["reason"]
    assert done["allocation"]["stopped"]
    assert done["instance_stop"] == "confirmed"
    assert done["instance_state"]["status"] == "shutdown"
    assert [call[2] for call in service.calls].count("create") == 1
    created = next(call for call in service.calls if call[2] == "create")
    for flag, value in [
        ("--gpu-spec", "TEST-gpu"),
        ("--image", "TEST-image"),
        ("--cuda-min", "118"),
        ("--gpus", "1"),
        ("--disk-gb", "0"),
    ]:
        assert created[created.index(flag) + 1] == value
    assert [call[2] for call in service.calls].count("stop") == 1
    assert len(scenario.executor.submissions) == 1
    assert platforms[0]["ssh"]["host"] == service.endpoint
    assert platforms[0]["autodl"]["instance"] == service.instance
    persisted = (scenario.store.directory(task["id"]) / "task.json").read_text()
    document = (scenario.store.directory(task["id"]) / "experiment.md").read_text()
    assert "TEST-PRIVATE" not in persisted + document
    assert "allocation" in document
    await scenario.flow.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission", ["allow_create", "allow_discovery", "allow_stop", "allow_execute"]
)
async def test_missing_authority_never_creates(tmp_path, monkeypatch, permission):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    task = await scenario.plan()
    platform = scenario.settings["platforms"]["cloud"]
    platform["ssh" if permission == "allow_execute" else "autodl"][permission] = False
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await scenario.idle(task["id"])
    assert paused["status"] == "paused"
    assert not service.calls
    assert "allocation" not in paused
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_changed_profile_invalidates_previous_approval_before_billing(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    task = await scenario.plan()
    config = scenario.settings["platforms"]["cloud"]["autodl"]
    config["resource_profiles"]["small"]["gpus"] = 2
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await scenario.idle(task["id"])
    assert "Resource profile changed" in paused["reason"]
    assert not service.calls
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_lost_create_response_is_found_and_stopped_without_duplicate_creation(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    service.create_error = True
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await scenario.idle(task["id"])
    assert paused["status"] == "paused"
    assert paused["allocation"]["instance"] == service.instance
    assert paused["allocation"]["stopped"]
    assert not scenario.executor.submissions
    assert [call[2] for call in service.calls].count("create") == 1
    await scenario.flow.interact(task["id"], OWNER, "continue")
    done = await scenario.idle(task["id"])
    assert done["status"] == "completed", done["reason"]
    assert [call[2] for call in service.calls].count("create") == 1
    assert "start" in [call[2] for call in service.calls]
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_restart_reconciles_pending_create_even_without_experiment_runs(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    service.create_error = True
    service.hidden = True
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await scenario.idle(task["id"])
    assert paused["allocation"]["state"] == "unknown"
    await scenario.flow.close()
    service.hidden = False
    scenario.flow = scenario.new_flow()
    await scenario.flow.recover()
    monitor = scenario.flow.monitors[task["id"]]
    await asyncio.wait_for(monitor, 5)
    restored = scenario.store.load(task["id"])
    assert restored["allocation"]["instance"] == service.instance
    assert restored["allocation"]["stopped"]
    assert [call[2] for call in service.calls].count("create") == 1
    assert not scenario.executor.submissions
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_unresolved_create_and_user_continue_never_create_another_instance(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    service.create_error = True
    service.hidden = True
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    await scenario.idle(task["id"])
    await scenario.flow.interact(task["id"], OWNER, "continue")
    paused = await scenario.idle(task["id"])
    assert paused["allocation"]["state"] == "unknown"
    assert not scenario.executor.submissions
    assert [call[2] for call in service.calls].count("create") == 1
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_user_cancel_during_ambiguous_create_reconciles_and_stops(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    service.create_wait = asyncio.Event()
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    await asyncio.wait_for(service.create_entered.wait(), 3)
    cancelled = await scenario.flow.interact(task["id"], OWNER, "stop")
    assert cancelled["status"] == "cancelled"
    assert cancelled["allocation"]["stopped"]
    assert not scenario.executor.submissions
    assert [call[2] for call in service.calls].count("create") == 1
    await scenario.flow.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["snapshot", "archive", "protected_endpoint"])
async def test_failures_still_shutdown_owned_instance(tmp_path, monkeypatch, failure):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    if failure == "snapshot":
        service.snapshot_error = True
    elif failure == "archive":
        scenario.executor.archive_failure = True
    else:
        service.endpoint = "10.8.176.151"
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await scenario.idle(task["id"])
    assert paused["status"] == "paused"
    assert paused["allocation"]["stopped"]
    assert [call[2] for call in service.calls].count("stop") == 1
    if failure != "archive":
        assert not scenario.executor.submissions
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_startup_deadline_and_preflight_failure_stop_without_running(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    scenario.settings["platforms"]["cloud"]["autodl"]["startup_seconds"] = 1
    service.state = "initializing"
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await scenario.idle(task["id"])
    assert "running status" in paused["reason"]
    assert paused["allocation"]["stopped"]
    assert not scenario.executor.submissions
    await scenario.flow.close()

    scenario, service = configured_scenario(tmp_path / "preflight", monkeypatch)

    async def failed_preflight():
        raise OSError("TEST SSH preflight unavailable")

    scenario.executor.preflight = failed_preflight
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await scenario.idle(task["id"])
    assert paused["allocation"]["stopped"]
    assert not scenario.executor.submissions
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_cancel_running_experiment_archives_best_effort_then_stops(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    scenario.executor.running = True
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    for _ in range(50):
        if scenario.executor.submissions:
            break
        await asyncio.sleep(0.02)
    assert scenario.executor.submissions
    cancelled = await scenario.flow.interact(task["id"], OWNER, "stop")
    assert cancelled["status"] == "cancelled"
    assert cancelled["allocation"]["stopped"]
    assert cancelled["runs"][0]["state"] == "interrupted"
    assert cancelled["runs"][0]["exit_code"] is None
    assert [call[2] for call in service.calls].count("create") == 1
    assert [call[2] for call in service.calls].count("stop") == 1
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_protected_static_host_is_rejected_before_any_paid_request(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    scenario.settings["platforms"]["cloud"]["ssh"]["host"] = "10.8.176.151"
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await scenario.idle(task["id"])
    assert "shared test server" in paused["reason"]
    assert not service.calls
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_disconnect_then_shutdown_reconciliation_does_not_query_powered_off_ssh(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    service.stop_pending = True
    scenario.executor.query_failures = 100
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await scenario.idle(task["id"])
    assert paused["instance_stop"] == "requested"
    assert len(scenario.executor.submissions) == 1
    remaining_failures = scenario.executor.query_failures
    await asyncio.sleep(1.2)
    assert scenario.executor.query_failures == remaining_failures
    service.state = "shutdown"
    await asyncio.wait_for(scenario.flow.monitors[task["id"]], 5)
    restored = scenario.store.load(task["id"])
    assert restored["allocation"]["stopped"]
    assert restored["runs"][0]["state"] == "interrupted"
    assert restored["status"] == "paused"
    assert len(scenario.executor.submissions) == 1
    assert [call[2] for call in service.calls].count("stop") == 1
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_native_question_stops_compute_and_reply_reuses_the_same_instance(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    entered, answered = asyncio.Event(), asyncio.Event()

    class QuestionHarnessDouble(ResourceHarnessDouble):
        """TEST DOUBLE: hold one native question open until the owner replies."""

        async def turn(self, prompt, timeout):
            if "\nPhase: implement\n" in prompt and not entered.is_set():
                entered.set()
                await self.emit(
                    {
                        "kind": "question",
                        "details": "TEST confirm the implementation detail",
                    }
                )
                await answered.wait()
            return await super().turn(prompt, timeout)

        async def answer(self, event, text):
            assert text == "TEST answer"
            answered.set()

    scenario.flow.harness_factory = lambda c, w, e: QuestionHarnessDouble(
        scenario, c, w, e
    )
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    await asyncio.wait_for(entered.wait(), 3)
    for _ in range(100):
        paused = scenario.store.load(task["id"])
        if paused["allocation"].get("stopped"):
            break
        await asyncio.sleep(0.02)
    assert paused["status"] == "waiting_user"
    assert paused["allocation"]["stopped"]
    assert not scenario.executor.submissions
    await scenario.flow.interact(task["id"], OWNER, "reply", text="TEST answer")
    done = await scenario.idle(task["id"])
    assert done["status"] == "completed", done["reason"]
    assert done["allocation"]["instance"] == paused["allocation"]["instance"]
    assert [call[2] for call in service.calls].count("create") == 1
    assert [call[2] for call in service.calls].count("stop") == 2
    await scenario.flow.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_mode", ["lost", "pending"])
async def test_shutdown_must_be_confirmed_and_ambiguous_stop_is_not_repeated(
    tmp_path, monkeypatch, stop_mode
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    service.stop_error = stop_mode == "lost"
    service.stop_pending = stop_mode == "pending"
    task = await scenario.plan()
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    paused = await scenario.idle(task["id"])
    assert paused["status"] == "paused"
    assert paused["archive_complete"]
    assert not paused["allocation"].get("stopped")
    assert [call[2] for call in service.calls].count("stop") == 1
    service.state = "shutdown"
    await asyncio.wait_for(scenario.flow.monitors[task["id"]], 5)
    done = scenario.store.load(task["id"])
    assert done["status"] == "completed"
    assert done["instance_stop"] == "confirmed"
    assert [call[2] for call in service.calls].count("stop") == 1
    await scenario.flow.close()


@pytest.mark.asyncio
async def test_paginated_discovery_refuses_duplicate_names(tmp_path, monkeypatch):
    scenario, _ = configured_scenario(tmp_path, monkeypatch)
    task = await scenario.plan()
    task["allocation"] = {"name": "astrbot-" + task["id"], "state": "submitting"}
    scenario.store.save(task)
    manager = Allocation(
        scenario.settings["platforms"]["cloud"], task, scenario.store.save
    )
    pages = []

    async def discovery(operation, **kwargs):
        pages.append(kwargs["page"])
        return {
            "instances": [
                {"uuid": f"TEST-{kwargs['page']}", "name": task["allocation"]["name"]}
            ],
            "max_page": 2,
        }

    monkeypatch.setattr(manager.manager, "invoke", discovery)
    with pytest.raises(RuntimeError, match="Multiple"):
        await manager.reconcile()
    assert pages == [1, 2]
    assert "instance" not in task["allocation"]
    await scenario.flow.close()


def test_resource_profiles_fail_closed_without_inventing_capacity():
    platform = {
        "type": "autodl",
        "autodl": {
            "resource_profiles": {
                "small": {"gpu_spec": "TEST", "image": "TEST", "cuda_min": 118}
            }
        },
    }
    plan = {"resources": {"profile": "missing", "reason": "TEST"}}
    with pytest.raises(ValueError, match="administrator-approved"):
        resource_spec(platform, plan)
    plan["resources"]["profile"] = "small"
    for key, value in [
        ("gpus", 5),
        ("cuda_min", 0),
        ("disk_gb", 501),
        ("region", "west"),
    ]:
        invalid = deepcopy(platform)
        invalid["autodl"]["resource_profiles"]["small"][key] = value
        with pytest.raises(ValueError):
            resource_spec(invalid, plan)
    platform["autodl"]["kind"] = "container"
    with pytest.raises(ValueError, match="Pro"):
        resource_spec(platform, plan)


@pytest.mark.asyncio
async def test_missing_cli_is_reported_before_persisting_a_creation_intent(
    tmp_path, monkeypatch
):
    scenario, service = configured_scenario(tmp_path, monkeypatch)
    task = await scenario.plan()
    task["approved_version"] = task["plan_version"]
    scenario.store.save(task)
    platform = scenario.settings["platforms"]["cloud"]
    platform["autodl"]["command"] = ["TEST-NONEXISTENT-AUTODL-EXECUTABLE"]
    monkeypatch.setattr(allocation_module, "AutoDL", AutoDL)
    with pytest.raises(RuntimeError, match="Install AutoDL CLI"):
        await Allocation(platform, task, scenario.store.save).prepare()
    assert "allocation" not in task
    assert not service.calls
    await scenario.flow.close()
