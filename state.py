"""Durable task records, separate research documents, and user constraints."""

import hashlib
import json
import re
import uuid
from pathlib import Path

from filelock import FileLock

TERMINAL = {"completed", "cancelled", "rejected"}
PHASES = ("research", "design", "prepare", "implement", "run", "analyze", "archive")
DEFAULT_LIMITS = {
    "model_calls": 30,
    "discussion_rounds": 2,
    "wall_seconds": 3600,
    "run_seconds": 600,
    "repair_rounds": 5,
    "network_retries": 2,
    "archive_bytes": 20_000_000,
    "tokens": 0,
}


class Store:
    """Keep atomic, versioned records without storing platform credentials.

    Args:
        root: Private plugin data directory.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, task_id: str) -> Path:
        """Resolve a validated task identifier inside the storage root.

        Args:
            task_id: Canonical hexadecimal UUID.

        Returns:
            Task directory.

        Raises:
            ValueError: If the identifier is invalid.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise ValueError("Invalid task identifier")
        directory = self.root / task_id
        if directory.resolve().parent != self.root or directory.is_symlink():
            raise ValueError("Task path escapes storage")
        return directory

    def create(self, owner: str, address: str, idea: str, settings: dict) -> dict:
        """Create a task with a private workspace and explicit resource limits.

        Args:
            owner: Platform-qualified authenticated sender identity.
            address: AstrBot message return address.
            idea: Original user input.
            settings: Non-secret task settings selected by the plugin.

        Returns:
            Persisted task record.
        """
        task_id = uuid.uuid4().hex
        directory = self.directory(task_id)
        (directory / "workspace").mkdir(parents=True)
        task = {
            "id": task_id,
            "revision": 0,
            "owner": owner,
            "address": address,
            "idea": idea,
            "supplements": [],
            "phase": "research",
            "status": "queued",
            "reason": "",
            "plan_version": 0,
            "approved_version": None,
            "plan": {},
            "participants": settings.get("participants", {}),
            "assignments": settings.get("assignments", {}),
            "platform": settings.get("platform", "local"),
            "platform_order": settings.get("platform_order", []),
            "platform_agents_file": settings.get("platform_agents_file", ""),
            "agents_file": settings.get("agents_file", ""),
            "agents": [],
            "sessions": {},
            "limits": {**DEFAULT_LIMITS, **settings.get("limits", {})},
            "usage": {
                "model_calls": 0,
                "repair_rounds": 0,
                "active_seconds": 0,
                "tokens_actual": 0,
                "tokens_cached": 0,
                "tokens_estimated": 0,
                "unknown_calls": 0,
            },
            "pending": None,
            "cursor": {},
            "handoff": "",
            "discussion": [],
            "runs": [],
            "manifest": [],
            "result": {},
            "notifications": [],
            "history": [],
        }
        for key, value in task["limits"].items():
            if key not in DEFAULT_LIMITS or (
                value is not None and (type(value) is not int or value < 0)
            ):
                raise ValueError("Resource limits must be nonnegative integers or null")
        self.save(task)
        return task

    def load(self, task_id: str, owner: str | None = None) -> dict:
        """Read a task and enforce its owner when called from a message.

        Args:
            task_id: Task identifier.
            owner: Authenticated requester, or None for internal scheduling.

        Returns:
            Task record.

        Raises:
            PermissionError: If the requester does not own the task.
        """
        directory = self.directory(task_id)
        with FileLock(str(directory / ".lock")):
            task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
        if owner is not None and task["owner"] != owner:
            raise PermissionError("Task belongs to another user")
        return task

    def save(self, task: dict) -> None:
        """Atomically checkpoint a record and render its separate task document.

        Args:
            task: Record with the last observed revision.

        Raises:
            RuntimeError: If another writer changed the record.
        """
        directory = self.directory(task["id"])
        path = directory / "task.json"
        with FileLock(str(directory / ".lock")):
            if path.exists():
                revision = json.loads(path.read_text(encoding="utf-8"))["revision"]
                if revision != task["revision"]:
                    raise RuntimeError("Task changed concurrently; reload before retry")
            task["revision"] += 1
            staging = directory / "task.json.tmp"
            staging.write_text(
                json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            staging.replace(path)
            document = {
                key: task[key]
                for key in (
                    "idea",
                    "supplements",
                    "plan_version",
                    "approved_version",
                    "plan",
                    "participants",
                    "assignments",
                    "phase",
                    "status",
                    "reason",
                    "discussion",
                    "usage",
                    "runs",
                    "manifest",
                    "result",
                )
            }
            for key in ("allocation", "instance_state", "instance_stop"):
                if key in task:
                    document[key] = task[key]
            for key in ("platform", "platform_order", "platform_selection", "agents"):
                document[key] = task.get(key)
            staging = directory / "experiment.md.tmp"
            staging.write_text(
                "# 科研任务\n\n```json\n"
                + json.dumps(document, ensure_ascii=False, indent=2)
                + "\n```\n",
                encoding="utf-8",
            )
            staging.replace(directory / "experiment.md")

    def list(self, owner: str | None = None) -> list[dict]:
        """List durable records without using model calls.

        Args:
            owner: Optional authenticated owner filter.

        Returns:
            Matching records.
        """
        tasks = []
        for path in sorted(self.root.glob("*/task.json")):
            task = self.load(path.parent.name)
            if owner is None or task["owner"] == owner:
                tasks.append(task)
        return tasks

    def constraints(self, task: dict) -> str:
        """Read applicable AGENTS files without modifying or generating them.

        Args:
            task: Task whose constraint fingerprints will be updated.

        Returns:
            Ordered shared constraints for every participant.

        Raises:
            ValueError: If a constraint file cannot be decoded.
        """
        workspace = self.directory(task["id"]) / "workspace"
        paths = [
            parent / "AGENTS.md"
            for parent in reversed((workspace, *workspace.parents))
            if (parent / "AGENTS.md").is_file()
        ]
        for selected in (task["agents_file"], task.get("platform_agents_file", "")):
            if selected:
                explicit = Path(selected).resolve(strict=True)
                if explicit not in paths:
                    paths.append(explicit)
        versions, sections = [], []
        for path in paths:
            raw = path.read_bytes()
            versions.append(
                {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
            )
            sections.append(
                f"Applicable constraints from {path}:\n{raw.decode('utf-8')}"
            )
        task["agents"] = versions
        return "\n\n".join(sections)
