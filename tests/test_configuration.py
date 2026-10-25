"""Validate native form settings using the real host and offline test doubles."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from astrbot.core.config.astrbot_config import AstrBotConfig

from astrbot_plugin_research.configuration import PHASES, read_configuration
from astrbot_plugin_research.state import DEFAULT_LIMITS
from astrbot_plugin_research.tests.test_workflow import OWNER, Scenario

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture
def form(tmp_path):
    return AstrBotConfig(str(tmp_path / "plugin.json"), schema=deepcopy(SCHEMA))


def member(name="builder", harness="codex", **values):
    """Build a card exactly as the native template editor does.

    Args:
        name: Unique member name.
        harness: Selected card template.
        **values: Explicit user overrides.

    Returns:
        Complete template entry with independent defaults.
    """
    result = {
        key: deepcopy(meta["default"])
        for key, meta in SCHEMA["participants"]["templates"][harness]["items"].items()
    }
    return {
        **result,
        "__template_key": harness,
        "name": name,
        "model": "TEST-MODEL",
        **values,
    }


def test_host_default_form_is_safe_and_loadable(form):
    settings, allowed = read_configuration(form)
    assert allowed == set()
    assert settings["participants"] == {}
    assert settings["platform"] == "auto"
    assert settings["limits"]["tokens"] == 0
    assert settings["limits"]["model_calls"] == 30
    for platform in settings["platforms"].values():
        assert not platform.get("allow_execute", False)
        assert not platform.get("ssh", {}).get("allow_execute", False)
    cloud = settings["platforms"]["autodl"]["autodl"]
    assert cloud["mode"] == "create"
    assert cloud["command"] == ["autodl"]
    assert all(
        cloud[key] is False
        for key in (
            "allow_create",
            "allow_discovery",
            "allow_start",
            "allow_stop",
            "start_on_prepare",
        )
    )


def test_real_dashboard_accepts_populated_cards(form):
    from astrbot.dashboard.services.config_service import validate_config

    form["users"] = [
        {
            "__template_key": "user",
            "platform_id": "TEST-platform",
            "sender_id": "TEST-user",
        }
    ]
    form["participants"] = [member(), member("reviewer", "deepseek")]
    form["autodl"]["resource_profiles"] = [
        {
            "__template_key": "profile",
            "name": "small",
            "description": "TEST ONLY",
            "gpu_spec": "TEST-GPU",
            "image": "TEST-IMAGE",
            "cuda_min": 118,
            "gpus": 1,
            "disk_gb": 0,
            "region": [],
        }
    ]
    errors, validated = validate_config(deepcopy(dict(form)), SCHEMA, is_core=False)
    assert errors == []
    assert validated["participants"] == form["participants"]
    assert (
        validated["autodl"]["resource_profiles"] == form["autodl"]["resource_profiles"]
    )


def test_host_upgrade_preserves_legacy_exactly(tmp_path):
    legacy = {
        "participants": {"original": {"harness": "codex", "model": "USER-CHOICE"}},
        "platform": "my_old_autodl",
        "platforms": {
            "my_old_autodl": {"type": "autodl", "autodl": {"allow_create": False}}
        },
        "future_or_custom_setting": {"preserved": True},
    }
    raw = json.dumps(legacy, ensure_ascii=False, indent=4)
    path = tmp_path / "existing.json"
    path.write_text(
        json.dumps({"settings_json": raw, "allowed_users": ['["p","u"]']}),
        encoding="utf-8",
    )
    upgraded = AstrBotConfig(str(path), schema=deepcopy(SCHEMA))
    upgraded["participants"] = [member()]
    upgraded["autodl"]["allow_create"] = True
    settings, allowed = read_configuration(upgraded)
    assert settings == legacy
    assert allowed == {'["p","u"]'}
    assert upgraded["settings_json"] == raw
    saved = json.loads(path.read_text(encoding="utf-8-sig"))
    assert saved["settings_json"] == raw
    assert saved["allowed_users"] == ['["p","u"]']


def test_form_explicitly_ignores_legacy_and_keeps_identity_scope(form):
    form["config_mode"] = "visual"
    form["settings_json"] = (
        '{"platform":"paid","platforms":{"paid":{"allow_execute":true}}}'
    )
    form["users"] = [
        {"__template_key": "user", "platform_id": " bot ", "sender_id": "123"},
        {"__template_key": "user", "platform_id": "other", "sender_id": "123"},
    ]
    form["allowed_users"] = ['["legacy","456"]']
    settings, allowed = read_configuration(form)
    assert "paid" not in settings["platforms"]
    assert not settings["platforms"]["local"]["allow_execute"]
    assert allowed == {'["bot","123"]', '["other","123"]', '["legacy","456"]'}
    form["settings_json"] = "malformed legacy"
    assert read_configuration(form)[0] == settings


@pytest.mark.parametrize("legacy", [{}, {"platform": "original", "custom": 123}])
def test_explicit_json_mode_never_uses_form(form, legacy):
    form["config_mode"] = "json"
    form["settings_json"] = json.dumps(legacy)
    form["participants"] = [member()]
    form["local"]["allow_execute"] = True
    assert read_configuration(form)[0] == legacy


def test_members_stages_models_argv_and_summary_order(form):
    form["participants"] = [
        member(
            order=20, provider="custom", program="C:/Tools/codex.exe", web_search="live"
        ),
        member(
            "reviewer",
            "deepseek",
            order=10,
            model="specific-model",
            provider="provider-key",
            program="C:/Program Files/nodejs/node.exe",
            arguments=["C:/DSH/lib/bin.js", "--profile", "acp"],
        ),
        member(
            "observer",
            "deepseek",
            order=30,
            stages=["design"],
            model='["opaque", "selector"]',
        ),
    ]
    before = deepcopy(dict(form))
    settings, _ = read_configuration(form)
    assert dict(form) == before
    assert settings["assignments"]["design"] == ["reviewer", "builder", "observer"]
    assert settings["assignments"]["summary"] == ["reviewer", "builder"]
    assert (
        settings["participants"]["reviewer"]["model"]
        == '["provider-key","specific-model"]'
    )
    assert settings["participants"]["observer"]["model"] == '["opaque", "selector"]'
    assert settings["participants"]["builder"]["command"] == [
        "C:/Tools/codex.exe",
        "app-server",
    ]
    assert settings["participants"]["builder"]["model_provider"] == "custom"
    assert settings["participants"]["builder"]["web_search"] == "live"
    assert settings["participants"]["reviewer"]["command"] == [
        "C:/Program Files/nodejs/node.exe",
        "C:/DSH/lib/bin.js",
        "--profile",
        "acp",
    ]


@pytest.mark.parametrize("platform", ["local", "ssh", "autodl"])
def test_platform_selection_and_independent_permissions(form, platform):
    form["platform"] = platform
    form["autodl"]["mode"] = "existing"
    form["autodl"]["kind"] = "container"
    form["autodl"]["stop_policy"] = "after_archive"
    form["autodl"]["instance"] = "TEST-EXISTING"
    form["autodl"]["program"] = "C:/Tools/autodl.exe"
    form["autodl"]["allow_discovery"] = True
    form["autodl"]["allow_stop"] = True
    form["ssh"].update(
        host="TEST.invalid", user="test", root="/test/tasks", allow_execute=True
    )
    settings, _ = read_configuration(form)
    assert settings["platform"] == platform
    assert settings["platforms"]["ssh"]["ssh"]["host"] == "TEST.invalid"
    assert settings["platforms"]["ssh"]["ssh"]["allow_execute"] is True
    cloud = settings["platforms"]["autodl"]["autodl"]
    assert cloud["command"] == ["C:/Tools/autodl.exe"]
    assert cloud["kind"] == "container"
    assert cloud["stop_policy"] == "after_archive"
    assert cloud["instance"] == "TEST-EXISTING"
    assert not cloud["allow_create"]
    assert not settings["platforms"]["autodl"]["ssh"]["allow_execute"]


def test_resource_cards_and_create_mode_keep_permissions_closed(form):
    form["platform"] = "autodl"
    form["autodl"]["resource_profiles"] = [
        {
            "__template_key": "profile",
            "name": "small",
            "description": "TEST ONLY",
            "gpu_spec": "TEST-GPU",
            "image": "TEST-PREPARED-IMAGE",
            "cuda_min": 118,
            "gpus": 1,
            "disk_gb": 0,
            "region": ["TEST-REGION"],
        }
    ]
    settings, _ = read_configuration(form)
    cloud = settings["platforms"]["autodl"]["autodl"]
    assert cloud["resource_profiles"]["small"] == {
        "description": "TEST ONLY",
        "gpu_spec": "TEST-GPU",
        "image": "TEST-PREPARED-IMAGE",
        "cuda_min": 118,
        "gpus": 1,
        "disk_gb": 0,
        "region": ["TEST-REGION"],
    }
    assert not cloud["allow_create"]
    assert not cloud["allow_stop"]
    assert not settings["platforms"]["autodl"]["ssh"]["allow_execute"]


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"participants": [member(), member()]}, "名称不能为空或重复"),
        ({"participants": [member(model="")]}, "模型标识"),
        ({"participants": [member(stages=["design"])]}, "想法完善与检索"),
        ({"participants": [member(stages=["invalid"])]}, "有效的负责阶段"),
        ({"participants": [member(arguments="app-server")]}, "启动参数"),
        ({"participants": [member(order=True)]}, "发言顺序"),
        ({"participants": [member(name="../builder")]}, "路径分隔符"),
        ({"platform": "unknown"}, "执行平台"),
        ({"limits": {"tokens": -1}}, "非负整数"),
        ({"concurrency": 0}, "至少为 1"),
        ({"users": [{"platform_id": "", "sender_id": "u"}]}, "平台 ID"),
        ({"config_mode": "auto", "settings_json": "[]"}, "必须是对象"),
        ({"config_mode": "auto", "settings_json": "{broken"}, "无法解析"),
        ({"config_mode": "unknown"}, "配置来源"),
    ],
)
def test_form_errors_are_actionable(form, patch, message):
    form.update(patch)
    with pytest.raises(ValueError, match=message):
        read_configuration(form)


def test_cloud_rejects_duplicate_profiles_and_container_creation(form):
    form["autodl"]["resource_profiles"] = [{"name": "same"}, {"name": "same"}]
    with pytest.raises(ValueError, match="名称不能为空或重复"):
        read_configuration(form)
    form["autodl"]["resource_profiles"] = []
    form["platform"] = "autodl"
    form["autodl"]["kind"] = "container"
    with pytest.raises(ValueError, match="仅支持 Pro"):
        read_configuration(form)
    form["autodl"]["kind"] = "pro"
    with pytest.raises(ValueError, match="至少需要一个资源档位"):
        read_configuration(form)
    form["autodl"]["resource_profiles"] = [{"name": "missing-spec"}]
    with pytest.raises(ValueError, match="请核对 GPU"):
        read_configuration(form)
    form["autodl"]["startup_seconds"] = 0
    with pytest.raises(ValueError, match="1–3600"):
        read_configuration(form)


def test_only_true_boolean_grants_permissions(form):
    form["local"]["allow_execute"] = "false"
    form["ssh"]["allow_execute"] = 1
    form["autodl"]["allow_create"] = "true"
    form["autodl"]["ssh"]["allow_execute"] = "true"
    settings, _ = read_configuration(form)
    assert not settings["platforms"]["local"]["allow_execute"]
    assert not settings["platforms"]["ssh"]["ssh"]["allow_execute"]
    assert not settings["platforms"]["autodl"]["autodl"]["allow_create"]
    assert not settings["platforms"]["autodl"]["ssh"]["allow_execute"]


def test_each_limit_has_an_independent_unlimited_switch(form):
    original = read_configuration(form)[0]["limits"]
    for key in DEFAULT_LIMITS:
        form["limits"][f"unlimited_{key}"] = True
        settings, _ = read_configuration(form)
        assert settings["limits"] == {**original, key: None}
        form["limits"][f"unlimited_{key}"] = False
    form["limits"]["repair_rounds"] = 0
    assert read_configuration(form)[0]["limits"]["repair_rounds"] == 0
    for key in DEFAULT_LIMITS:
        form["limits"][f"unlimited_{key}"] = True
    assert all(
        value is None for value in read_configuration(form)[0]["limits"].values()
    )


def test_priority_dropdowns_and_per_platform_rules(form):
    form["routing"].update(first="ssh", second="local", third="none")
    for platform in ("local", "ssh", "autodl"):
        form[platform]["agents_file"] = f"C:/TEST/{platform}-AGENTS.md"
        form[platform]["capabilities"] = f"TEST {platform} capability declaration"
    before = deepcopy(dict(form))
    settings, _ = read_configuration(form)
    assert dict(form) == before
    assert settings["platform_order"] == ["ssh", "local"]
    for name, platform in settings["platforms"].items():
        assert platform["agents_file"] == f"C:/TEST/{name}-AGENTS.md"
        assert platform["capabilities"] == f"TEST {name} capability declaration"
    form["routing"].update(first="local", second="local")
    with pytest.raises(ValueError, match="不能重复"):
        read_configuration(form)
    form["routing"].update(first="none", second="none", third="none")
    with pytest.raises(ValueError, match="至少选择一个平台"):
        read_configuration(form)


@pytest.mark.asyncio
async def test_visual_form_drives_complete_offline_workflow(form, tmp_path):
    scenario = Scenario(tmp_path / "tasks")
    form["participants"] = [
        member(stages=["design", "implement", "summary"]),
        member("reviewer", "deepseek", stages=["research", "design", "analyze"]),
    ]
    form["local"]["allow_execute"] = True
    form["platform"] = "local"
    scenario.settings, _ = read_configuration(form)
    scenario.flow = scenario.new_flow()
    task = await scenario.plan()
    assert task["status"] == "waiting_confirmation"
    assert scenario.executor.submissions == []
    assert set(task["assignments"]) == set(PHASES)
    await scenario.flow.interact(task["id"], OWNER, "approve", version=1)
    done = await scenario.idle(task["id"])
    assert done["status"] == "completed"
    assert done["result"]["verdict"] == "no_improvement"
    assert done["manifest"]
    assert scenario.messages


@pytest.mark.asyncio
async def test_real_entry_uses_visual_members_and_user_cards(
    form, tmp_path, monkeypatch
):
    from astrbot_plugin_research.main import Main
    from astrbot_plugin_research.main import star as host_star

    monkeypatch.setattr(
        host_star.StarTools, "get_data_dir", lambda _: tmp_path / "entry"
    )
    form["participants"] = [member()]
    form["users"] = [{"__template_key": "user", "platform_id": "p", "sender_id": "u"}]
    context = MagicMock()
    context.send_message = AsyncMock()
    plugin = Main(context, form)
    await plugin.initialize()
    plugin.workflow.schedule = MagicMock()
    event = MagicMock()
    event.is_admin.return_value = False
    event.get_platform_id.return_value = "p"
    event.get_sender_id.return_value = "u"
    event.unified_msg_origin = "TEST:p:u"
    event.plain_result.side_effect = lambda text: text
    replies = [reply async for reply in plugin.research(event, "new TEST visual entry")]
    task = plugin.store.list()[0]
    assert task["id"] in replies[0]
    assert task["participants"]["builder"]["model"] == "TEST-MODEL"
    assert task["assignments"]["summary"] == ["builder"]
    assert task["owner"] == '["p","u"]'
    event.should_call_llm.assert_called_once_with(False)
    await plugin.terminate()
