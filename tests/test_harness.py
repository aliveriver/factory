"""Offline adapter contracts; RPC fixtures do not represent real integrations."""

from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_research.harness import CodexHarness, DeepSeekHarness


@pytest.mark.asyncio
async def test_codex_explicit_model_pinned_sandbox_native_questions_and_usage(tmp_path):
    sink = AsyncMock()
    harness = CodexHarness({}, tmp_path, sink)
    rpc = AsyncMock()

    async def call(method, params, **kwargs):
        if method == "model/list":
            return {"data": [{"model": "TEST-model"}], "nextCursor": None}
        if method == "thread/resume":
            assert params["model"] == "TEST-model"
            assert params["sandbox"] == "read-only"
            assert params["approvalsReviewer"] == "user"
            assert not params["config"]["features.shell_tool"]
            return {"thread": {"id": "TEST-thread"}}
        return {}

    rpc.call.side_effect = call
    harness.rpc = rpc
    info = await harness.open("TEST-model", "TEST-thread")
    assert info["configured"] == "TEST-model"
    assert not info["verified"]
    await harness.receive(
        {
            "id": "TEST-approval",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "TEST-thread"},
        }
    )
    assert sink.call_args.args[0]["kind"] == "approval"
    await harness.answer(sink.call_args.args[0], "not an explicit grant")
    assert rpc.send.call_args.args[0]["result"] == {"decision": "decline"}
    await harness.receive(
        {
            "id": "TEST-question",
            "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "metric", "question": "Metric?"}]},
        }
    )
    assert sink.call_args.args[0]["kind"] == "question"
    await harness.answer(sink.call_args.args[0], "accuracy")
    assert rpc.send.call_args.args[0]["result"] == {
        "answers": {"metric": {"answers": ["accuracy"]}}
    }
    await harness.receive(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "tokenUsage": {"last": {"totalTokens": 30, "cachedInputTokens": 10}}
            },
        }
    )
    assert harness.usage == {"actual": 30, "cached": 10}
    await harness.receive({"id": 999, "method": "unknown/clientRequest"})
    assert rpc.send.call_args.args[0]["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_deepseek_opaque_models_empty_resume_capability_and_no_fallback(tmp_path):
    harness = DeepSeekHarness({}, tmp_path, AsyncMock())
    rpc = AsyncMock()
    chosen = []

    async def call(method, params, **kwargs):
        if method == "initialize":
            return {"agentCapabilities": {"sessionCapabilities": {"resume": {}}}}
        if method == "session/resume":
            return {
                "configOptions": [
                    {
                        "id": "model",
                        "options": [
                            {
                                "group": "TEST",
                                "options": [
                                    {
                                        "value": '["TEST","exact-model"]',
                                        "name": "Exact model",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        if method == "session/set_config_option":
            chosen.append(params["value"])
        return {}

    rpc.call.side_effect = call
    harness.rpc = rpc
    info = await harness.open('["TEST","exact-model"]', "TEST-session")
    assert info["reference"] == "TEST-session"
    assert info["capabilities"]["resume"]
    assert chosen == ['["TEST","exact-model"]']
    with pytest.raises(ValueError, match="catalog"):
        await harness.open("unavailable-model", "TEST-session")
    assert chosen == ['["TEST","exact-model"]']
    request = {
        "request_id": 3,
        "details": {
            "options": [
                {"kind": "allow_once", "optionId": "TEST-allow"},
                {"kind": "reject_once", "optionId": "TEST-deny"},
            ]
        },
    }
    await harness.answer(request, "deny")
    assert rpc.send.call_args.args[0]["result"] == {
        "outcome": {"outcome": "selected", "optionId": "TEST-deny"}
    }
    await harness.receive(
        {
            "method": "session/update",
            "params": {"update": {"sessionUpdate": "usage_update", "used": 1234}},
        }
    )
    assert harness.usage is None
    assert harness.capabilities["context_occupancy"] == 1234
    await harness.cancel()
    assert rpc.send.call_args.args[0]["method"] == "session/cancel"


@pytest.mark.asyncio
async def test_codex_correlates_early_completion_and_ignores_other_turns(tmp_path):
    harness = CodexHarness({}, tmp_path, AsyncMock())
    harness.session_id = "TEST-thread"
    harness.model = "TEST-model"
    rpc = AsyncMock()

    async def call(method, params, **kwargs):
        await harness.receive(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "OTHER-thread",
                    "turnId": "TEST-turn",
                    "item": {"type": "agentMessage", "text": "FOREIGN"},
                },
            }
        )
        await harness.receive(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "TEST-thread",
                    "turnId": "OLD-turn",
                    "item": {"type": "agentMessage", "text": "STALE"},
                },
            }
        )
        await harness.receive(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "TEST-thread",
                    "turnId": "TEST-turn",
                    "item": {"type": "agentMessage", "text": "MATCHED"},
                },
            }
        )
        await harness.receive(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "TEST-thread",
                    "turn": {"id": "TEST-turn", "status": "completed"},
                },
            }
        )
        return {"turn": {"id": "TEST-turn"}}

    rpc.call.side_effect = call
    harness.rpc = rpc
    assert (await harness.turn("TEST input", 1))["text"] == "MATCHED"
