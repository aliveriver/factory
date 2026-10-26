"""Explicitly authorized local, SSH, and AutoDL execution."""

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

PROTECTED_HOST = "10.8.176.151"
LOGIN_COMMAND = (
    "ssh duankaidi@10.8.176.151 'mkdir -- \"$HOME/astrbot-plugin-login-test-20261024\"'"
)
RUNNER = Path(__file__).with_name("runner.py")
FINAL_RUN_STATES = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}


async def command(
    argv: list[str],
    timeout: float = 30,
    env: dict | None = None,
    output_limit: int = 4_000_000,
) -> bytes:
    """Execute an argv with bounded output and no credential-bearing errors.

    Args:
        argv: Program and literal arguments.
        timeout: Maximum transport duration.
        env: Optional private process environment.
        output_limit: Maximum captured bytes.

    Returns:
        Standard output on success.

    Raises:
        RuntimeError: If execution fails or output exceeds the limit.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:

        async def read(stream):
            """Bound a command pipe before buffering its full contents."""
            chunks = bytearray()
            while chunk := await stream.read(65536):
                chunks.extend(chunk)
                if len(chunks) > output_limit:
                    raise RuntimeError("Command output limit exceeded")
            return bytes(chunks)

        stdout, _ = await asyncio.wait_for(
            asyncio.gather(read(process.stdout), read(process.stderr)), timeout
        )
        code = await asyncio.wait_for(process.wait(), timeout)
        if code:
            raise RuntimeError(f"Platform command failed with exit code {code}")
        return stdout
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class LocalExecutor:
    """Run authorized commands in durable, separately supervised processes.

    Args:
        config: Administrator-controlled local execution permission.
    """

    kind = "local"

    def __init__(self, config: dict):
        self.config = config

    async def preflight(self) -> dict:
        """Check local permission without model calls."""
        if not self.config.get("allow_execute", False):
            raise PermissionError("Local experiment execution has not been authorized")
        return {"python": sys.version.split()[0], "kind": self.kind}

    async def submit(
        self, workspace: Path, run_id: str, argv: list[str], timeout: float | None
    ) -> dict:
        """Persist an execution intent and launch an idempotent supervisor.

        Args:
            workspace: Authorized task workspace.
            run_id: Previously persisted unique run identifier.
            argv: Approved reproduction command as an argument list.
            timeout: Supervisor-enforced runtime limit, or None for no limit.

        Returns:
            Stable execution reference, independent of the Bot process.
        """
        await self.preflight()
        directory = workspace / ".runs" / run_id
        reference = {
            "id": run_id,
            "directory": str(directory),
            "state": "submitted",
            "source_files": [],
        }
        if directory.exists():
            return reference
        directory.mkdir(parents=True)
        source_root = directory / "source"
        source_root.mkdir()
        for source in sorted(workspace.rglob("*")):
            relative = source.relative_to(workspace)
            if ".runs" in relative.parts:
                continue
            if source.is_symlink() or not source.resolve().is_relative_to(workspace):
                raise ValueError("Source snapshot refuses symlinks or escaping paths")
            if source.is_file():
                reference["source_files"].append(relative.as_posix())
                target = directory / "source" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        (directory / "request.json").write_text(
            json.dumps(
                {
                    "id": run_id,
                    "argv": argv,
                    "timeout": timeout,
                    "cwd": str(source_root),
                }
            ),
            encoding="utf-8",
        )
        # A detached supervisor enforces timeout even if AstrBot exits.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(RUNNER),
            str(directory),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {
                    "PATH",
                    "SYSTEMROOT",
                    "WINDIR",
                    "COMSPEC",
                    "PATHEXT",
                    "TEMP",
                    "TMP",
                    "HOME",
                    "USERPROFILE",
                    "LANG",
                    "LC_ALL",
                }
            },
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0
            ),
        )
        reference["supervisor_pid"] = process.pid
        return reference

    async def query(self, reference: dict) -> dict:
        """Read the original run; missing evidence never triggers another launch.

        Args:
            reference: Previously submitted execution reference.

        Returns:
            Durable run state or unknown when launch outcome is ambiguous.
        """
        path = Path(reference["directory"]) / "status.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        request = path.with_name("request.json")
        if request.exists() and time.time() - request.stat().st_mtime < 5:
            return {"id": reference["id"], "state": "starting"}
        return {"id": reference["id"], "state": "unknown"}

    async def cancel(self, reference: dict) -> None:
        """Request supervisor cancellation, without assuming it has completed.

        Args:
            reference: Existing run reference.
        """
        (Path(reference["directory"]) / "cancel").touch()

    async def collect(
        self, workspace: Path, reference: dict, artifacts: list[str], limit: int | None
    ) -> list[dict]:
        """Record available local evidence with integrity hashes.

        Args:
            workspace: Task workspace.
            reference: Completed execution reference.
            artifacts: Approved relative artifact paths.
            limit: Total evidence size ceiling, or None for no limit.

        Returns:
            Integrity-checked archive entries.
        """
        entries = []
        total = 0
        archive = workspace / ".runs" / reference["id"] / "archive"
        for name in [
            f".runs/{reference['id']}/stdout.log",
            f".runs/{reference['id']}/stderr.log",
            f".runs/{reference['id']}/status.json",
            f".runs/{reference['id']}/request.json",
            *[
                f".runs/{reference['id']}/source/{name}"
                for name in reference.get("source_files", [])
            ],
            *artifacts,
        ]:
            source = workspace / name
            if name in artifacts:
                source = workspace / ".runs" / reference["id"] / "source" / name
            path = archive / name
            if not path.exists():
                path = source
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(workspace.resolve())
                or not path.is_file()
            ):
                entries.append({"path": name, "status": "missing"})
                continue
            expected_size = path.stat().st_size
            total += expected_size
            if limit is not None and total > limit:
                entries.append({"path": name, "status": "not_saved", "reason": "size"})
                continue
            target = archive / name
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            remaining = None if limit is None else limit - total + expected_size
            if target != path:
                staging = target.with_name(target.name + ".tmp")
                with path.open("rb") as source_stream, staging.open("wb") as output:
                    for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        if remaining is not None and size > remaining:
                            break
                        digest.update(chunk)
                        output.write(chunk)
            else:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        if remaining is not None and size > remaining:
                            break
                        digest.update(chunk)
            if remaining is not None and size > remaining:
                if target != path:
                    staging.unlink()
                entries.append({"path": name, "status": "not_saved", "reason": "size"})
                continue
            if target != path:
                staging.replace(target)
            total += size - expected_size
            entries.append(
                {
                    "path": name,
                    "status": "downloaded",
                    "sha256": digest.hexdigest(),
                    "bytes": size,
                    "local": str(target),
                }
            )
        return entries


class SSHExecutor:
    """Operate only on explicitly authorized POSIX/Python SSH destinations.

    Args:
        config: Host, user, port, identity path, work root, and authorization.
        transport: Injectable local command runner for offline contract tests.
    """

    kind = "ssh"

    def __init__(self, config: dict, transport=command, require_execute: bool = True):
        self.config = config
        self.transport = transport
        host = config.get("host", "")
        if host == PROTECTED_HOST or PROTECTED_HOST in json.dumps(config):
            raise PermissionError("The shared test server forbids generic operations")
        # Literal destinations only; SSH config aliases/proxies cannot bypass policy.
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host):
            raise ValueError("A literal authorized SSH host is required")
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", config.get("user", "")):
            raise ValueError("An SSH user is required")
        root = PurePosixPath(config.get("root", ""))
        if not root.is_absolute() or ".." in root.parts or str(root) == "/":
            raise ValueError("SSH root must be a dedicated absolute directory")
        if require_execute and not config.get("allow_execute", False):
            raise PermissionError("SSH execution has not been authorized")
        self.destination = f"{config['user']}@{host}"
        self.root = root
        self.python = config.get("python", "python3")
        self.resolved = False

    def options(self, scp: bool = False) -> list[str]:
        """Build pinned noninteractive SSH options with user config disabled.

        Args:
            scp: Select the scp port flag.

        Returns:
            Non-secret transport arguments.
        """
        args = [
            "-F",
            "NUL" if os.name == "nt" else "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking="
            + (
                "accept-new"
                if self.config.get("accept_new_host_keys", False)
                else "yes"
            ),
            "-o",
            "ConnectTimeout=10",
            "-P" if scp else "-p",
            str(int(self.config.get("port", 22))),
        ]
        if not self.resolved:
            addresses = socket.getaddrinfo(
                self.config["host"],
                int(self.config.get("port", 22)),
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
            if any(item[4][0] == PROTECTED_HOST for item in addresses):
                raise PermissionError(
                    "SSH destination resolves to the protected server"
                )
            address = addresses[0][4][0]
            self.destination = f"{self.config['user']}@{address}"
            self.resolved = True
        args.extend(["-o", f"HostKeyAlias={self.config['host']}"])
        if scp:
            # Legacy SCP gives remote path quoting a consistent POSIX-shell meaning.
            args.append("-O")
        if self.config.get("identity_file"):
            args.extend(["-i", str(Path(self.config["identity_file"]).resolve())])
        return args

    async def remote(self, script: str) -> bytes:
        """Send one shell-quoted command only to the configured destination.

        Args:
            script: Adapter-generated POSIX command.

        Returns:
            Bounded standard output.
        """
        return await self.transport(
            ["ssh", *self.options(), self.destination, script], timeout=30
        )

    async def preflight(self) -> dict:
        """Check authorized Python availability; never check remote harnesses."""
        response = await self.remote(f"{shlex.quote(self.python)} --version")
        return {"kind": self.kind, "python": response.decode().strip()}

    async def inspect_environment(self) -> dict:
        """Read remote Python metadata without changing the remote host.

        Returns:
            Python, operating system, executable and installed distribution metadata.

        Raises:
            ValueError: If the fixed inspection response is not valid JSON.
        """
        script = (
            "import importlib.metadata,json,platform,sys; "
            "packages=[]; "
            "packages=[{'name':d.metadata.get('Name',''),"
            "'version':d.version} for d in importlib.metadata.distributions() "
            "if d.metadata.get('Name')]; "
            "print(json.dumps({'python':sys.version,"
            "'executable':sys.executable,'platform':platform.platform(),"
            "'machine':platform.machine(),'packages':packages},"
            "ensure_ascii=False))"
        )
        response = await self.remote(
            f"{shlex.quote(self.python)} -c {shlex.quote(script)}"
        )
        try:
            result = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Remote Python inspection returned invalid JSON") from exc
        if not isinstance(result, dict) or not isinstance(result.get("packages"), list):
            raise ValueError("Remote Python inspection returned an invalid shape")
        result["kind"] = self.kind
        return result

    async def upload(self, source: Path, target: PurePosixPath) -> None:
        """Upload one file to the explicitly authorized task root.

        Args:
            source: Local regular file.
            target: Absolute remote destination below the configured root.
        """
        if not target.is_relative_to(self.root) or ".." in target.parts:
            raise ValueError("Upload target escapes SSH root")
        await self.transport(
            [
                "scp",
                *self.options(scp=True),
                str(source),
                f"{self.destination}:{shlex.quote(str(target))}",
            ],
            timeout=30,
        )

    async def submit(
        self, workspace: Path, run_id: str, argv: list[str], timeout: float | None
    ) -> dict:
        """Upload a versioned workspace and claim a durable run once.

        Args:
            workspace: Local task workspace.
            run_id: Persisted unique run identifier.
            argv: Approved argv, using paths relative to the remote workspace.
            timeout: Remote supervisor deadline, or None for no limit.

        Returns:
            Stable remote execution reference.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Invalid SSH run identifier")
        remote = self.root / run_id
        reference = {
            "id": run_id,
            "directory": str(remote),
            "state": "submitted",
            "source_files": [],
        }
        # Failure, including an existing directory, is ambiguous: query, never replay.
        await self.remote(
            f"mkdir -p -- {shlex.quote(str(self.root))} && "
            f"mkdir -- {shlex.quote(str(remote))}"
        )
        evidence = workspace / ".runs" / run_id
        evidence.mkdir(parents=True, exist_ok=True)
        request = evidence / "request.json"
        request.write_text(
            json.dumps(
                {"id": run_id, "argv": argv, "timeout": timeout, "cwd": str(remote)}
            ),
            encoding="utf-8",
        )
        for source in sorted(workspace.rglob("*")):
            relative = source.relative_to(workspace)
            if ".runs" in relative.parts:
                continue
            if source.is_symlink() or not source.resolve().is_relative_to(workspace):
                raise ValueError("SSH upload refuses symlinks or escaping paths")
            if source.is_file():
                reference["source_files"].append(relative.as_posix())
                target = remote / relative.as_posix()
                await self.remote(f"mkdir -p -- {shlex.quote(str(target.parent))}")
                await self.upload(source, target)
        await self.upload(RUNNER, remote / ".astrbot-runner.py")
        await self.upload(request, remote / "request.json")
        await self.remote(
            f"nohup {shlex.quote(self.python)} "
            f"{shlex.quote(str(remote / '.astrbot-runner.py'))} "
            f"{shlex.quote(str(remote))} </dev/null "
            f">{shlex.quote(str(remote / 'launcher.log'))} 2>&1 &"
        )
        return reference

    async def query(self, reference: dict) -> dict:
        """Query existing run evidence after disconnect, without launching work.

        Args:
            reference: Durable reference saved before submission.

        Returns:
            Supervisor status or an explicit unknown state.
        """
        path = PurePosixPath(reference["directory"]) / "status.json"
        if not path.is_relative_to(self.root) or ".." in path.parts:
            raise ValueError("Run reference escapes SSH root")
        result = await self.remote(
            f"if test -f {shlex.quote(str(path))}; then "
            f"cat -- {shlex.quote(str(path))}; else "
            f"printf '%s' '{{\"state\":\"unknown\"}}'; fi"
        )
        return json.loads(result)

    async def cancel(self, reference: dict) -> None:
        """Signal the supervisor using its cancellation file.

        Args:
            reference: Previously recorded remote execution.
        """
        path = PurePosixPath(reference["directory"]) / "cancel"
        if not path.is_relative_to(self.root) or ".." in path.parts:
            raise ValueError("Cancellation path escapes SSH root")
        await self.remote(f"touch -- {shlex.quote(str(path))}")

    async def collect(
        self, workspace: Path, reference: dict, artifacts: list[str], limit: int | None
    ) -> list[dict]:
        """Download bounded evidence and compare remote and local SHA-256.

        Args:
            workspace: Local task workspace.
            reference: Existing remote run.
            artifacts: Approved relative files.
            limit: Maximum aggregate download size, or None for no limit.

        Returns:
            Downloaded, remote-only, and failed archive entries.
        """
        remote = PurePosixPath(reference["directory"])
        if not remote.is_relative_to(self.root) or ".." in remote.parts:
            raise ValueError("Evidence reference escapes SSH root")
        local = workspace / ".runs" / reference["id"] / "download"
        local.mkdir(parents=True, exist_ok=True)
        entries, total = [], 0
        for name in dict.fromkeys(
            [
                "stdout.log",
                "stderr.log",
                "status.json",
                "request.json",
                *reference.get("source_files", []),
                *artifacts,
            ]
        ):
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name:
                raise ValueError("Evidence paths must be relative")
            path = remote / relative
            entry = {"path": name, "remote": str(path), "status": "remote_only"}
            try:
                script = (
                    "import pathlib,hashlib,json\n"
                    f"r=pathlib.Path({str(remote)!r}).resolve()\n"
                    f"p=pathlib.Path({str(path)!r})\n"
                    "assert not p.is_symlink() and p.resolve().is_relative_to(r)\n"
                    "h=hashlib.sha256()\n"
                    "with p.open('rb') as stream:\n"
                    " for chunk in iter(lambda:stream.read(1048576),b''):\n"
                    "  h.update(chunk)\n"
                    "print(json.dumps({'bytes':p.stat().st_size,'sha256':h.hexdigest()}))"
                )
                metadata = json.loads(
                    await self.remote(
                        f"{shlex.quote(self.python)} -c {shlex.quote(script)}"
                    )
                )
                total += metadata["bytes"]
                entry.update(metadata)
                if limit is not None and total > limit:
                    entry["reason"] = "size"
                    entries.append(entry)
                    continue
                target = local / name
                target.parent.mkdir(parents=True, exist_ok=True)
                await self.transport(
                    [
                        "scp",
                        *self.options(scp=True),
                        f"{self.destination}:{shlex.quote(str(path))}",
                        str(target),
                    ],
                    timeout=30,
                )
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if (
                    target.stat().st_size != metadata["bytes"]
                    or digest.hexdigest() != metadata["sha256"]
                ):
                    raise ValueError("Evidence checksum mismatch")
                entry.update(status="downloaded", local=str(target))
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                entry.update(status="not_saved", reason=type(exc).__name__)
            entries.append(entry)
        return entries


class AutoDL:
    """Use saurlax/autodl-cli for lifecycle only; experiments still use SSH.

    Args:
        config: CLI executable, instance kind/ID, credential environment reference,
            and independent discovery/create/start/stop permissions.
        transport: Injectable command transport.
    """

    def __init__(self, config: dict, transport=command):
        self.config = config
        self.transport = transport
        if config.get("kind", "pro") not in {"pro", "container"}:
            raise ValueError("AutoDL kind must be pro or container")
        if config.get("stop_policy", "leave") not in {
            "leave",
            "after_archive",
            "always",
        }:
            raise ValueError("Unsupported AutoDL stop policy")

    async def invoke(
        self,
        operation: str,
        instance: str | None = None,
        *,
        resources: dict | None = None,
        name: str = "",
        page: int = 1,
    ) -> dict:
        """Perform an allowed operation without retrying ambiguous mutations.

        Args:
            operation: list, get, create, status, start, or stop.
            instance: Explicit instance identifier, except for list and create.
            resources: Administrator-approved creation parameters.
            name: Unique persisted allocation name for reconciliation.
            page: Page of instance discovery to retrieve.

        Returns:
            CLI API response with only non-secret status information.

        Raises:
            PermissionError: If this operation lacks independent authorization.
            ValueError: If the operation or instance is invalid.
        """
        if operation not in {"list", "get", "create", "status", "start", "stop"}:
            raise ValueError("Unsupported AutoDL operation; release is disabled")
        permission = (
            "allow_discovery"
            if operation in {"list", "get", "status"}
            else f"allow_{operation}"
        )
        if not self.config.get(permission, False):
            raise PermissionError(f"AutoDL {operation} has not been authorized")
        if operation not in {"list", "create"} and (
            not instance or not re.fullmatch(r"[a-zA-Z0-9_-]+", instance)
        ):
            raise ValueError("An explicit existing AutoDL instance is required")
        group = "instance" if self.config.get("kind", "pro") == "pro" else "container"
        args = [*self.config.get("command", ["autodl"]), group, operation]
        if instance:
            args.append(instance)
        if operation == "create":
            if group != "instance":
                raise ValueError("Automatic creation supports Pro instances only")
            if not resources or not re.fullmatch(r"astrbot-[0-9a-f]{32}", name):
                raise ValueError(
                    "Approved resources and an allocation name are required"
                )
            for key in ("gpu_spec", "image", "cuda_min", "gpus", "disk_gb"):
                value = resources[key]
                if not isinstance(value, (str, int)) or str(value).startswith("-"):
                    raise ValueError("Invalid AutoDL creation parameter")
                args.extend(["--" + key.replace("_", "-"), str(value)])
            if resources.get("region"):
                args.extend(["--region", ",".join(resources["region"])])
            args.extend(["--name", name])
        if operation == "list" and page != 1:
            args.extend(["--page", str(page)])
        args.extend(["--json", "--timeout", "20s"])
        # Secrets are resolved only into a child environment, never an argv or record.
        environment = os.environ.copy()
        token_reference = self.config.get("token_env")
        if token_reference:
            if not environment.get(token_reference):
                raise RuntimeError("AutoDL credential environment reference is empty")
            key = "AUTODL_TOKEN" if group == "instance" else "AUTODL_WEB_TOKEN"
            environment[key] = environment[token_reference]
        raw = json.loads(await self.transport(args, env=environment, timeout=25))
        if raw.get("code") != "Success":
            raise RuntimeError("AutoDL API rejected the request")
        if operation == "list":
            # List payloads can contain passwords and notebook tokens.
            data = raw.get("data", [])
            if isinstance(data, dict):
                data = data.get("list", data.get("instances", []))
            result = {
                "instances": [
                    {k: item[k] for k in ("uuid", "status", "name") if k in item}
                    for item in data
                    if isinstance(item, dict)
                ]
            }
            payload = raw.get("data", {})
            result["max_page"] = (
                payload.get("max_page", 1) if isinstance(payload, dict) else 1
            )
            return result
        data = raw.get("data")
        if operation == "create":
            if not isinstance(data, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", data):
                raise RuntimeError(
                    "AutoDL create returned no valid instance identifier"
                )
            return {"instance": data}
        if operation == "get":
            # Discard connection passwords and notebook tokens before returning.
            if not isinstance(data, dict):
                raise RuntimeError("AutoDL snapshot is unavailable")
            host, port = data.get("proxy_host"), data.get("ssh_port")
            if (
                not isinstance(host, str)
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host)
                or type(port) is not int
                or not 1 <= port <= 65535
                or host == PROTECTED_HOST
            ):
                raise ValueError("AutoDL returned an invalid or protected SSH endpoint")
            return {"host": host, "port": port}
        if operation == "status" and isinstance(data, dict):
            data = data.get("status", "unknown")
        return {"status": data if operation == "status" else "requested"}

    async def finish(self, archive_ok: bool) -> dict:
        """Apply a configured stop policy even when evidence collection fails.

        Args:
            archive_ok: Whether necessary artifacts were saved locally.

        Returns:
            Explicit instance state, separately from task and process state.
        """
        policy = self.config.get("stop_policy", "leave")
        should_stop = policy == "always" or (policy == "after_archive" and archive_ok)
        if should_stop:
            return await self.invoke("stop", self.config.get("instance"))
        return {
            "status": "left_running",
            "reason": "archive_incomplete" if not archive_ok else policy,
        }


def make_executor(config: dict):
    """Construct an explicitly authorized execution adapter.

    Args:
        config: Administrator platform configuration.

    Returns:
        Local executor or shared SSH path for SSH and AutoDL.

    Raises:
        ValueError: If platform type is unsupported.
    """
    if config.get("type") == "local":
        return LocalExecutor(config)
    if config.get("type") in {"ssh", "autodl"}:
        return SSHExecutor(config.get("ssh", config))
    raise ValueError("Unsupported experiment platform")
