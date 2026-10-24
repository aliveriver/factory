"""Controlled execution tests; never connect to the shared server or AutoDL."""

import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path

import psutil
import pytest

from astrbot_plugin_research.execution import (
    AutoDL,
    LocalExecutor,
    SSHExecutor,
    command,
)


class SSHTransportDouble:
    """TEST DOUBLE: virtual remote files and SSH/scp argv, with no SSH process."""

    def __init__(self):
        self.calls = []
        self.files = {
            "stdout.log": b"TEST remote output",
            "stderr.log": b"",
            "status.json": b'{"state":"succeeded"}',
            "metrics.json": b'{"baseline":1,"modified":1}',
            "experiment.py": b"print('TEST')",
            "request.json": b'{"argv":["python3","experiment.py"]}',
        }
        self.disconnect = False
        self.bad_checksum = False
        self.collection_failure = False

    async def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.disconnect:
            self.disconnect = False
            raise OSError("TEST disconnect, no real server")
        if argv[0] == "scp":
            if "@192.0.2.10:" in argv[-2]:
                if self.collection_failure:
                    raise OSError("TEST download failure")
                name = argv[-2].strip("'").split("/")[-1].strip("'")
                Path(argv[-1]).write_bytes(
                    b"TEST CORRUPTION" if self.bad_checksum else self.files[name]
                )
            return b""
        script = argv[-1]
        if "--version" in script:
            return b"Python TEST"
        if "hashlib" in script:
            name = next(n for n in self.files if n in script)
            data = self.files[name]
            return json.dumps(
                {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            ).encode()
        if "cat --" in script:
            return b'{"state":"succeeded","exit_code":0}'
        return b""


@pytest.mark.asyncio
async def test_ssh_offline_upload_query_disconnect_cancel_and_integrity(tmp_path):
    transport = SSHTransportDouble()
    executor = SSHExecutor(
        {
            "host": "192.0.2.10",
            "user": "test",
            "root": "/TEST space/experiments",
            "allow_execute": True,
        },
        transport,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "experiment.py").write_text("print('TEST')")
    assert (await executor.preflight())["python"] == "Python TEST"
    run_id = uuid.uuid4().hex
    reference = await executor.submit(
        workspace, run_id, ["python3", "experiment.py"], 2
    )
    assert any(c[0] == "scp" for c in transport.calls)
    assert any("nohup" in c[-1] for c in transport.calls)
    assert not any("codex" in c[-1] or "dsh " in c[-1] for c in transport.calls)
    assert all("-F" in c for c in transport.calls)
    submissions = len([c for c in transport.calls if "nohup" in c[-1]])
    transport.disconnect = True
    with pytest.raises(OSError):
        await executor.query(reference)
    assert (await executor.query(reference))["state"] == "succeeded"
    assert len([c for c in transport.calls if "nohup" in c[-1]]) == submissions
    await executor.cancel(reference)
    assert "touch --" in transport.calls[-1][-1]
    manifest = await executor.collect(workspace, reference, ["metrics.json"], 10000)
    assert all(item["status"] == "downloaded" for item in manifest)
    transport.bad_checksum = True
    manifest = await executor.collect(workspace, reference, [], 10000)
    assert all(item["status"] == "not_saved" for item in manifest)
    transport.bad_checksum = False
    manifest = await executor.collect(workspace, reference, [], 1)
    assert any(item["status"] == "remote_only" for item in manifest)
    with pytest.raises(ValueError):
        await executor.collect(workspace, reference, ["../escape"], 10000)


def test_shared_server_fails_closed_before_any_generic_operation():
    transport = SSHTransportDouble()
    with pytest.raises(PermissionError, match="shared"):
        SSHExecutor(
            {
                "host": "10.8.176.151",
                "user": "duankaidi",
                "root": "/tmp/test",
                "allow_execute": True,
            },
            transport,
        )
    assert not transport.calls
    with pytest.raises(PermissionError):
        SSHExecutor(
            {
                "host": "192.0.2.10",
                "user": "test",
                "root": "/tmp/test",
                "allow_execute": False,
            },
            transport,
        )
    for root in ["/", "../escape", "/tmp/../etc"]:
        with pytest.raises(ValueError):
            SSHExecutor(
                {
                    "host": "192.0.2.10",
                    "user": "test",
                    "root": root,
                    "allow_execute": True,
                },
                transport,
            )


@pytest.mark.asyncio
async def test_alias_of_protected_server_never_reaches_transport(monkeypatch):
    import socket

    transport = SSHTransportDouble()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.8.176.151", 22))
        ],
    )
    executor = SSHExecutor(
        {
            "host": "protected-alias.example",
            "user": "test",
            "root": "/TEST/experiments",
            "allow_execute": True,
        },
        transport,
    )
    with pytest.raises(PermissionError, match="protected"):
        await executor.preflight()
    assert not transport.calls


@pytest.mark.asyncio
async def test_local_source_and_artifact_versions_survive_workspace_changes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    code = (
        "from pathlib import Path\nPath('metrics.json').write_text('{\"score\":1}')\n"
    )
    (workspace / "experiment.py").write_text(code, encoding="utf-8")
    original = (workspace / "experiment.py").read_bytes()
    executor = LocalExecutor({"allow_execute": True})
    reference = await executor.submit(
        workspace, uuid.uuid4().hex, [sys.executable, "experiment.py"], 10
    )
    (workspace / "experiment.py").write_text("raise RuntimeError('new version')")
    for _ in range(100):
        state = await executor.query(reference)
        if state["state"] == "succeeded":
            break
        await asyncio.sleep(0.05)
    assert state["state"] == "succeeded"
    manifest = await executor.collect(workspace, reference, ["metrics.json"], 10000)
    source = next(
        item for item in manifest if item["path"].endswith("source/experiment.py")
    )
    assert source["sha256"] == hashlib.sha256(original).hexdigest()
    metric = next(item for item in manifest if item["path"] == "metrics.json")
    run_source = Path(reference["directory"]) / "source"
    (run_source / "metrics.json").write_text('{"score":999}')
    retried = await executor.collect(workspace, reference, ["metrics.json"], 10000)
    preserved = next(item for item in retried if item["path"] == "metrics.json")
    assert preserved["sha256"] == metric["sha256"]


@pytest.mark.asyncio
async def test_real_local_deadline_cancel_and_recovery(tmp_path):
    """Use real local Python processes; this is not an SSH or harness test."""
    executor = LocalExecutor({"allow_execute": True})
    workspace = tmp_path / "controlled"
    workspace.mkdir()
    run_id = uuid.uuid4().hex
    ref = await executor.submit(
        workspace, run_id, [sys.executable, "-c", "import time; time.sleep(20)"], 0.4
    )
    fresh = LocalExecutor({"allow_execute": True})
    for _ in range(100):
        state = await fresh.query(ref)
        if state["state"] == "timed_out":
            break
        await asyncio.sleep(0.05)
    assert state["state"] == "timed_out"
    same = await fresh.submit(workspace, run_id, ["never-relaunch"], 100)
    assert same["directory"] == ref["directory"]
    assert (await fresh.query(ref))["state"] == "timed_out"
    second = await fresh.submit(
        workspace,
        uuid.uuid4().hex,
        [sys.executable, "-c", "import time; time.sleep(20)"],
        10,
    )
    await fresh.cancel(second)
    for _ in range(100):
        state = await fresh.query(second)
        if state["state"] == "cancelled":
            break
        await asyncio.sleep(0.05)
    assert state["state"] == "cancelled"


@pytest.mark.asyncio
async def test_supervisor_does_not_leave_background_descendants(tmp_path):
    """Verify actual local process-tree cleanup; no remote system is involved."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "spawn.py").write_text(
        "import subprocess,sys\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        "Path('child.pid').write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    executor = LocalExecutor({"allow_execute": True})
    reference = await executor.submit(
        workspace, uuid.uuid4().hex, [sys.executable, "spawn.py"], 5
    )
    for _ in range(100):
        state = await executor.query(reference)
        if state["state"] == "succeeded":
            break
        await asyncio.sleep(0.05)
    assert state["state"] == "succeeded"
    child_pid = int((Path(reference["directory"]) / "source" / "child.pid").read_text())
    for _ in range(100):
        if not psutil.pid_exists(child_pid):
            break
        try:
            if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("Controlled experiment descendant survived supervisor exit")


@pytest.mark.asyncio
async def test_autodl_offline_contract_permissions_secrets_and_stop_policy(monkeypatch):
    calls = []

    async def transport(argv, **kwargs):
        calls.append((argv, kwargs))
        if "list" in argv:
            return json.dumps(
                {
                    "code": "Success",
                    "data": {
                        "list": [
                            {
                                "uuid": "TEST-id",
                                "status": "stopped",
                                "ssh_password": "TEST secret",
                                "jupyter_token": "TEST secret",
                            }
                        ]
                    },
                }
            ).encode()
        return b'{"code":"Success","data":"running"}'

    monkeypatch.setenv("TEST_AUTODL_SECRET_REF", "TEST-credential")
    config = {
        "kind": "pro",
        "instance": "TEST-id",
        "token_env": "TEST_AUTODL_SECRET_REF",
        "allow_discovery": True,
        "allow_start": False,
        "allow_stop": True,
        "stop_policy": "always",
    }
    adapter = AutoDL(config, transport)
    discovered = await adapter.invoke("list")
    assert "password" not in json.dumps(discovered)
    assert "TEST secret" not in json.dumps(discovered)
    assert calls[-1][0] == ["autodl", "instance", "list", "--json", "--timeout", "20s"]
    assert "TEST-credential" not in " ".join(calls[-1][0])
    assert calls[-1][1]["env"]["AUTODL_TOKEN"] == "TEST-credential"
    with pytest.raises(PermissionError):
        await adapter.invoke("start", "TEST-id")
    with pytest.raises(ValueError):
        await adapter.invoke("release", "TEST-id")
    assert (await adapter.finish(archive_ok=False))["status"] == "requested"
    assert calls[-1][0][1:4] == ["instance", "stop", "TEST-id"]
    config["stop_policy"] = "after_archive"
    assert (await adapter.finish(False))["status"] == "left_running"
    config["kind"] = "container"
    await adapter.invoke("status", "TEST-id")
    assert calls[-1][0][1:3] == ["container", "status"]
    assert calls[-1][1]["env"]["AUTODL_WEB_TOKEN"] == "TEST-credential"
    config["allow_start"] = True

    async def ambiguous(argv, **kwargs):
        calls.append((argv, kwargs))
        raise OSError("TEST ambiguous response")

    adapter.transport = ambiguous
    count = len(calls)
    with pytest.raises(OSError):
        await adapter.invoke("start", "TEST-id")
    assert len(calls) == count + 1


@pytest.mark.asyncio
async def test_bounded_command_capture_and_error_redaction():
    with pytest.raises(RuntimeError, match="output limit"):
        await command([sys.executable, "-c", "print('x'*10000)"], output_limit=100)
    with pytest.raises(RuntimeError, match="exit code") as error:
        await command(
            [
                sys.executable,
                "-c",
                "import sys;sys.stderr.write('TEST-SECRET');sys.exit(2)",
            ]
        )
    assert "TEST-SECRET" not in str(error.value)
