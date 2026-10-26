"""Controlled discovery contracts; no live SSH or Harness process is used."""

import json

import pytest

from astrbot_plugin_research.execution import SSHExecutor
from astrbot_plugin_research.harness import CodexHarness, DeepSeekHarness


class DiscoverySSHDouble:
    """TEST DOUBLE: returns fixed remote inspection JSON without SSH."""

    def __init__(self):
        self.calls = []

    async def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        script = argv[-1]
        assert "importlib.metadata" in script
        assert "pip install" not in script
        return json.dumps(
            {
                "python": "3.12.1",
                "executable": "/opt/venv/bin/python3",
                "platform": "Linux-TEST",
                "machine": "x86_64",
                "packages": [{"name": "numpy", "version": "TEST"}],
            }
        ).encode()


@pytest.mark.asyncio
async def test_ssh_discovery_is_read_only_and_does_not_require_execution():
    transport = DiscoverySSHDouble()
    executor = SSHExecutor(
        {
            "host": "192.0.2.10",
            "user": "test",
            "root": "/TEST/discovery",
            "allow_execute": False,
        },
        transport,
        require_execute=False,
    )
    result = await executor.inspect_environment()
    assert result["packages"][0]["name"] == "numpy"
    assert len(transport.calls) == 1
    assert "mkdir" not in transport.calls[0][-1]


class HarnessRPCDouble:
    """TEST DOUBLE: emulates model catalog initialization only."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def start(self):
        pass

    async def call(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "model/list":
            return self.response
        if method == "initialize":
            return {"agentCapabilities": {"sessionCapabilities": {}}}
        if method == "session/new":
            return {
                "configOptions": [
                    {
                        "id": "model",
                        "options": [
                            {
                                "options": [
                                    {
                                        "value": "TEST-provider/model",
                                        "name": "TEST model",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }

    async def send(self, message):
        self.calls.append(("send", message))

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_codex_discovery_normalizes_model_values(tmp_path):
    harness = CodexHarness({}, tmp_path, lambda event: None)
    harness.rpc = HarnessRPCDouble(
        {
            "data": [{"slug": "TEST-codex", "name": "TEST Codex"}],
            "nextCursor": None,
        }
    )
    models = await harness.discover_models()
    assert models == [{"value": "TEST-codex", "name": "TEST Codex", "description": ""}]
    assert [call[0] for call in harness.rpc.calls] == [
        "initialize",
        "send",
        "model/list",
    ]


@pytest.mark.asyncio
async def test_deepseek_discovery_returns_opaque_acp_values(tmp_path):
    harness = DeepSeekHarness({}, tmp_path, lambda event: None)
    harness.rpc = HarnessRPCDouble({})
    models = await harness.discover_models()
    assert models == [{"value": "TEST-provider/model", "name": "TEST model"}]
    assert [call[0] for call in harness.rpc.calls] == ["initialize", "session/new"]
