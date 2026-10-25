"""Approved AutoDL resource selection and durable instance ownership."""

import asyncio
import shutil
import time
from copy import deepcopy
from pathlib import Path

from .execution import AutoDL, SSHExecutor, command


def resource_spec(platform: dict, plan: dict) -> dict:
    """Resolve an approved resource profile without accepting arbitrary CLI flags.

    Args:
        platform: Administrator-owned platform configuration.
        plan: Proposed plan with a resource profile and selection rationale.

    Returns:
        A complete, non-secret creation specification for plan approval.

    Raises:
        ValueError: If the profile or resource values are invalid.
    """
    if platform.get("type") != "autodl":
        raise ValueError("Resource allocation requires an AutoDL platform")
    config = platform["autodl"]
    if config.get("kind", "pro") != "pro":
        raise ValueError("Automatic instance creation requires AutoDL Pro")
    selection = plan.get("resources", {})
    if not isinstance(selection, dict) or not selection.get("reason"):
        raise ValueError("Plan must explain its selected resource profile")
    profile = config.get("resource_profiles", {}).get(selection.get("profile"))
    if not isinstance(profile, dict):
        raise ValueError("Plan must select an administrator-approved resource profile")
    spec = {
        key: profile.get(key, default)
        for key, default in (
            ("gpu_spec", ""),
            ("image", ""),
            ("cuda_min", 0),
            ("gpus", 1),
            ("disk_gb", 0),
            ("region", []),
        )
    }
    for key in ("gpu_spec", "image"):
        if (
            not isinstance(spec[key], str)
            or not spec[key].strip()
            or spec[key].startswith("-")
            or "\x00" in spec[key]
            or any(character.isspace() for character in spec[key])
        ):
            raise ValueError(f"Resource profile needs an explicit {key}")
    for key, lower, upper in (
        ("cuda_min", 1, 9999),
        ("gpus", 1, 4),
        ("disk_gb", 0, 500),
    ):
        if type(spec[key]) is not int or not lower <= spec[key] <= upper:
            raise ValueError(f"Resource profile has an invalid {key}")
    if not isinstance(spec["region"], list) or any(
        not isinstance(region, str)
        or not region
        or region.startswith("-")
        or "," in region
        or "\x00" in region
        or any(character.isspace() for character in region)
        for region in spec["region"]
    ):
        raise ValueError("Resource regions must be a list of data center codes")
    return spec


def task_platform(platform: dict, task: dict) -> dict:
    """Bind execution and shutdown to the persisted task-owned instance.

    Args:
        platform: Current administrator platform settings.
        task: Persisted task containing an optional allocation.

    Returns:
        Independent platform settings with only non-secret endpoint overrides.
    """
    result = deepcopy(platform)
    allocation = task.get("allocation")
    if allocation:
        if (
            result.get("type") != "autodl"
            or not isinstance(result.get("autodl"), dict)
            or not isinstance(result.get("ssh"), dict)
        ):
            raise ValueError("Restore the task's AutoDL platform before reconciliation")
        result["autodl"]["instance"] = allocation.get("instance")
        result["autodl"]["stop_policy"] = "always"
        result["ssh"].update(allocation.get("endpoint", {}))
    return result


class Allocation:
    """Reconcile one task-owned instance without repeating uncertain creation.

    Args:
        platform: Administrator-owned AutoDL platform configuration.
        task: Mutable durable task.
        save: Synchronous checkpoint callback.
    """

    def __init__(self, platform: dict, task: dict, save):
        self.platform = platform
        self.config = platform["autodl"]
        self.task = task
        self.save = save
        self.manager = AutoDL(self.config)

    async def reconcile(self) -> bool:
        """Find a previously submitted create by its unique persisted name.

        Returns:
            Whether the original allocation now has a known instance identifier.

        Raises:
            RuntimeError: If discovery is incomplete or more than one instance matches.
        """
        allocation = self.task["allocation"]
        if allocation.get("instance"):
            return True
        matches = []
        page, pages = 1, 1
        while page <= pages:
            result = await self.manager.invoke("list", page=page)
            pages = result.get("max_page", 1)
            if type(pages) is not int or not 1 <= pages <= 100:
                raise RuntimeError("Allocation discovery pagination is incomplete")
            matches.extend(
                instance["uuid"]
                for instance in result["instances"]
                if instance.get("name") == allocation["name"] and instance.get("uuid")
            )
            page += 1
        matches = list(dict.fromkeys(matches))
        if len(matches) > 1:
            raise RuntimeError(
                "Multiple instances match; manual reconciliation required"
            )
        if not matches:
            allocation["state"] = "unknown"
            self.save(self.task)
            return False
        allocation.update(instance=matches[0], state="created")
        self.save(self.task)
        return True

    async def prepare(self) -> dict:
        """Create only after approval, wait for startup, and retain a safe endpoint.

        Returns:
            Task-bound platform settings for the SSH executor.

        Raises:
            PermissionError: If creation, discovery, shutdown, or execution is disabled.
            RuntimeError: If allocation, startup, or a previous stop remains uncertain.
        """
        if self.task["approved_version"] != self.task["plan_version"]:
            raise PermissionError("Instance creation requires current plan approval")
        for permission in ("allow_create", "allow_discovery", "allow_stop"):
            if not self.config.get(permission, False):
                raise PermissionError(f"Automatic allocation requires {permission}")
        if not self.platform.get("ssh", {}).get("allow_execute", False):
            raise PermissionError(
                "Automatic allocation requires SSH execution permission"
            )
        # Validate static SSH policy before a billable call; do not connect or resolve.
        ssh = self.platform["ssh"]
        SSHExecutor({**ssh, "host": ssh.get("host") or "pending.autodl.invalid"})
        if ssh.get("identity_file") and not Path(ssh["identity_file"]).is_file():
            raise ValueError("SSH identity file does not exist")
        program = self.config.get("command", ["autodl"])[0]
        if self.manager.transport is command and shutil.which(program) is None:
            raise RuntimeError(
                "Install AutoDL CLI and configure its executable before creating"
            )
        timeout = self.config.get("startup_seconds", 300)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError("startup_seconds must be between 1 and 3600")
        shutdown_timeout = self.config.get("shutdown_seconds", 120)
        if type(shutdown_timeout) is not int or not 1 <= shutdown_timeout <= 3600:
            raise ValueError("shutdown_seconds must be between 1 and 3600")
        spec = resource_spec(self.platform, self.task["plan"])
        if spec != self.task["plan"]["resources"].get("spec"):
            raise RuntimeError("Resource profile changed; revise and approve the plan")
        allocation = self.task.get("allocation")
        if allocation is None:
            # A failed credential/read check is safe to retry before a create intent.
            await self.manager.invoke("list")
            allocation = {
                "name": "astrbot-" + self.task["id"],
                "spec": spec,
                "state": "submitting",
                "created_at": time.time(),
            }
            self.task["allocation"] = allocation
            self.save(self.task)
            # Checkpoint before issuing the billable request; exceptions never retry it.
            result = await self.manager.invoke(
                "create", resources=spec, name=allocation["name"]
            )
            allocation.update(instance=result["instance"], state="created")
            self.save(self.task)
        elif allocation["spec"] != spec:
            raise RuntimeError(
                "Existing allocation uses other resources; create a new task"
            )
        elif not await self.reconcile():
            raise RuntimeError(
                "Create remains uncertain; inspect the allocation name, never resubmit"
            )
        if self.task.get("instance_stop") and not allocation.get("stopped"):
            raise RuntimeError("Previous shutdown is unconfirmed; reconcile it first")
        instance = allocation["instance"]
        deadline = time.monotonic() + timeout
        while True:
            state = await self.manager.invoke("status", instance)
            self.task["instance_state"] = state
            self.save(self.task)
            if state["status"] == self.config.get("running_status", "running"):
                allocation["stopped"] = False
                allocation.pop("shutdown_started_at", None)
                self.task.pop("instance_start", None)
                self.task.pop("instance_stop", None)
                break
            if allocation.get("stopped"):
                if not self.config.get("allow_start", False):
                    raise PermissionError(
                        "Resuming the allocated instance requires allow_start"
                    )
                if not self.task.get("instance_start"):
                    self.task["instance_start"] = "submitting"
                    self.save(self.task)
                    await self.manager.invoke("start", instance)
                    self.task["instance_start"] = "requested"
                    self.save(self.task)
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Allocated instance did not reach its running status"
                )
            await asyncio.sleep(1)
        allocation["endpoint"] = await self.manager.invoke("get", instance)
        allocation["state"] = "ready"
        self.save(self.task)
        return task_platform(self.platform, self.task)

    async def stop(self) -> bool:
        """Request shutdown once and confirm it through read-only status queries.

        Returns:
            Whether platform status confirms instance shutdown.
        """
        allocation = self.task["allocation"]
        allocation.setdefault("shutdown_started_at", time.time())
        self.save(self.task)
        if not await self.reconcile():
            self.task["instance_state"] = {
                "status": "unknown",
                "allocation": allocation["name"],
            }
            self.save(self.task)
            return False
        instance = allocation["instance"]
        state = await self.manager.invoke("status", instance)
        self.task["instance_state"] = state
        if state["status"] in self.config.get(
            "stopped_statuses", ["shutdown", "stopped"]
        ):
            allocation.update(state="stopped", stopped=True)
            self.task["instance_stop"] = "confirmed"
            self.save(self.task)
            return True
        if not self.task.get("instance_stop"):
            self.task["instance_stop"] = "submitting"
            self.save(self.task)
            await self.manager.invoke("stop", instance)
            self.task["instance_stop"] = "requested"
        self.save(self.task)
        return False
