"""Load the real AstrBot entry and validate message routing without a live Bot."""

import importlib
import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot_plugin_research.tests.test_workflow import Scenario


@pytest.mark.asyncio
async def test_real_entry_load_message_persistence_and_default_llm_bypass(
    tmp_path, monkeypatch
):
    module = importlib.import_module("astrbot_plugin_research.main")
    monkeypatch.setattr(module.star.StarTools, "get_data_dir", lambda _: tmp_path)
    context = MagicMock()
    context.send_message = AsyncMock()
    scenario = Scenario(tmp_path / "fixtures")
    plugin = module.Main(
        context, {"settings_json": json.dumps(scenario.settings), "allowed_users": []}
    )
    await plugin.initialize()
    plugin.workflow.schedule = MagicMock()
    event = MagicMock()
    event.is_admin.return_value = True
    event.get_platform_id.return_value = "test-platform"
    event.get_sender_id.return_value = "owner"
    event.unified_msg_origin = "test-platform:FriendMessage:owner"
    event.plain_result.side_effect = lambda text: text
    replies = [r async for r in plugin.research(event, "new TEST entry load")]
    assert len(replies) == 1
    event.stop_event.assert_called_once()
    event.should_call_llm.assert_called_once_with(False)
    task = plugin.store.list()[0]
    assert task["idea"] == "TEST entry load"
    assert task["owner"] == '["test-platform","owner"]'
    assert plugin.store.load(task["id"])["address"] == event.unified_msg_origin
    responses = [r async for r in plugin.research(event, f"status {task['id']}")]
    assert task["id"] in responses[0]
    event.get_sender_id.return_value = "other"
    responses = [r async for r in plugin.research(event, f"status {task['id']}")]
    assert "belongs to another" in responses[0]
    await plugin.terminate()


@pytest.mark.asyncio
async def test_entry_access_and_task_settings_are_owner_checked(tmp_path, monkeypatch):
    module = importlib.import_module("astrbot_plugin_research.main")
    monkeypatch.setattr(module.star.StarTools, "get_data_dir", lambda _: tmp_path)
    plugin = module.Main(MagicMock(), {"settings_json": "{}", "allowed_users": []})
    event = MagicMock()
    event.is_admin.return_value = False
    event.get_platform_id.return_value = "p"
    event.get_sender_id.return_value = "u"
    event.plain_result.side_effect = lambda text: text
    results = [r async for r in plugin.research(event, "new not authorized")]
    assert "access is not configured" in results[0]
    assert plugin.store.list() == []


@pytest.mark.asyncio
async def test_harness_save_rejects_unverified_or_empty_model(tmp_path, monkeypatch):
    """Keep the persisted participant list unchanged when model selection is invalid."""
    module = importlib.import_module("astrbot_plugin_research.main")
    monkeypatch.setattr(module.star.StarTools, "get_data_dir", lambda _: tmp_path)
    config = {
        "config_mode": "visual",
        "participants": [
            {
                "__template_key": "codex",
                "name": "existing",
                "model": "verified-model",
                "stages": ["research", "design", "implement", "analyze", "summary"],
            }
        ],
        "allowed_users": [],
    }
    plugin = module.Main(MagicMock(), config)
    original = deepcopy(config["participants"])

    async def call(payload):
        module.request.json = AsyncMock(return_value=payload)
        response = await plugin.configure_harness()
        assert response.status_code == 400

    await call(
        {
            "kind": "codex",
            "name": "new",
            "model": "",
            "discovery_id": "missing",
        }
    )
    assert config["participants"] == original

    plugin.discovery_cache["known"] = {
        "kind": "codex",
        "models": [{"label": "Known", "value": "known-model"}],
    }
    await call(
        {
            "kind": "codex",
            "name": "new",
            "model": "",
            "discovery_id": "known",
        }
    )
    assert config["participants"] == original

    await call(
        {
            "kind": "codex",
            "name": "new",
            "model": "unverified-model",
            "discovery_id": "known",
        }
    )
    assert config["participants"] == original
