"""Research orchestration with durable gates and bounded model work."""

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

from .execution import FINAL_RUN_STATES, AutoDL, make_executor
from .harness import make_harness
from .state import PHASES, TERMINAL, Store

PLAN_FIELDS = (
    "baseline",
    "change",
    "data",
    "metric",
    "scale",
    "decision_rule",
    "resource_policy",
    "commands",
    "artifacts",
)
OUTPUT_CONTRACT = """
Return exactly one JSON object, without Markdown fences.
For research: summary, evidence (source, fact, author_conclusion, inference,
impact), question (only when necessary), disagreements, needs_confirmation.
For discussion: summary, evidence, disagreements (only unresolved material issues).
For design synthesis: summary, disagreements, plan containing baseline, change,
data, metric, scale, decision_rule, resource_policy, commands (lists of argv),
artifacts (relative files needed to verify results).
Do not invent a baseline, dataset, measurement, external capability or success.
For implement/repair: summary, files (relative path to complete UTF-8 content).
Implement the approved plan only; never change its comparison, metric or scope.
For analyze: verdict (improved, no_improvement, insufficient_evidence, blocked),
summary, baseline_metrics, modified_metrics, limitations, key_disagreements.
No improvement is a valid result, not a program failure or a reason to tune.
Do not execute commands or modify files yourself. Return changes in files.
Never use SSH, scp, platform CLIs, credentials or remote tools. The plugin alone
owns experiment execution, resource limits, transport, polling and archives.
Proposed commands must run in the foreground; do not detach background jobs.
User AGENTS constraints below apply unchanged to all participants.
"""


class Workflow:
    """Coordinate serial participants and experiments without a default chat LLM.

    Args:
        store: Durable task storage.
        settings: Administrator-owned plugin settings.
        notify: Message delivery callback; failures remain queryable.
        harness_factory: Production adapter factory or explicitly injected test double.
        executor_factory: Production executor factory or injected test double.
    """

    def __init__(
        self,
        store: Store,
        settings: dict,
        notify: Callable[[dict, str], Awaitable[None]],
        harness_factory=make_harness,
        executor_factory=make_executor,
    ):
        self.store = store
        self.settings = settings
        self.notify = notify
        self.harness_factory = harness_factory
        self.executor_factory = executor_factory
        self.workers = {}
        self.live = {}
        self.tasks = {}
        self.monitors = {}
        self.semaphore = asyncio.Semaphore(max(1, settings.get("concurrency", 1)))
        self.closed = False

    def create(self, owner: str, address: str, idea: str) -> dict:
        """Accept an idea using administrator-selected, non-secret task defaults.

        Args:
            owner: Authenticated platform/user identity.
            address: Message destination.
            idea: Unmodified original idea.

        Returns:
            Created durable record.
        """
        participants = self.settings.get("participants", {})
        if not participants:
            raise ValueError("Configure at least one real harness/model participant")
        safe = {}
        for name, participant in participants.items():
            if participant.get("harness") not in {"codex", "deepseek"}:
                raise ValueError("Production participants must use codex or deepseek")
            if not participant.get("model"):
                raise ValueError("Every participant needs an explicit model")
            safe[name] = {
                key: participant[key]
                for key in ("harness", "model", "role")
                if key in participant
            }
        assignments = self.settings.get("assignments", {})
        for phase in ("research", "design", "implement", "analyze", "summary"):
            selected = assignments.get(phase)
            if not selected or any(name not in safe for name in selected):
                raise ValueError(f"Configure valid participant assignments for {phase}")
        task = self.store.create(
            owner,
            address,
            idea,
            {
                "participants": safe,
                "assignments": assignments,
                "platform": self.settings.get("platform", "local"),
                "agents_file": self.settings.get("agents_file", ""),
                "limits": self.settings.get("limits", {}),
            },
        )
        self.store.constraints(task)
        self.store.save(task)
        return task

    def schedule(self, task_id: str) -> None:
        """Schedule one task at most once within this Bot process.

        Args:
            task_id: Durable task ID.
        """
        if self.closed or task_id in self.workers:
            return
        worker = asyncio.create_task(self.drive(task_id))
        self.workers[task_id] = worker
        worker.add_done_callback(lambda _: self.workers.pop(task_id, None))

    async def announce(self, task: dict, text: str) -> None:
        """Persist an outbox entry before attempting proactive notification.

        Args:
            task: Current task.
            text: Short notification, also accessible by task ID.
        """
        entry = {"text": text, "delivered": False}
        task["notifications"].append(entry)
        self.store.save(task)
        try:
            await self.notify(task, text)
        except Exception as exc:
            entry["error"] = type(exc).__name__
        else:
            entry["delivered"] = True
        self.store.save(task)

    async def interact(
        self,
        task_id: str,
        owner: str,
        action: str,
        text: str = "",
        version: int | None = None,
    ) -> dict:
        """Apply owner messages without approving stale plans or replaying runs.

        Args:
            task_id: Task ID.
            owner: Authenticated user identity.
            action: reply, approve, reject, supplement, continue, or stop.
            text: Reply or supplement.
            version: Explicit version required for approval/rejection.

        Returns:
            Updated durable task.

        Raises:
            ValueError: If the operation conflicts with the current gate.
        """
        task = self.tasks.get(task_id) or self.store.load(task_id, owner)
        if task["owner"] != owner:
            raise PermissionError("Task belongs to another user")
        if action == "stop":
            worker = self.workers.get(task_id)
            if worker:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            task = self.store.load(task_id, owner)
            task["status"] = "cancelled"
            task["reason"] = (
                "Stopped by owner; process and instance states are separate"
            )
            self.store.save(task)
            await self.settle(task, cancel=True)
            await self.announce(task, self.status(task))
            return task
        pending = task.get("pending")
        if action == "reply" and pending and pending.get("native"):
            live = self.live.get((task_id, pending["participant"]))
            if live is None:
                raise ValueError(
                    "Native request expired after restart; continue the saved session"
                )
            if text == "allow":
                raise ValueError(
                    "Native tool execution is disabled in this release; "
                    "return code through the structured files interface"
                )
            await live.answer(pending["event"], text)
            task["pending"] = None
            task["status"] = "active"
            self.store.save(task)
            return task
        if task_id in self.workers:
            raise ValueError(
                "Task is active; wait for a checkpoint before editing, or submit a new task"
            )
        if task["status"] in TERMINAL:
            raise ValueError("This task has ended; submit a new idea")
        if action in {"approve", "reject"}:
            if (
                task["status"] != "waiting_confirmation"
                or version != task["plan_version"]
            ):
                raise ValueError("Reply must name the current waiting plan version")
            if action == "reject":
                task["status"] = "rejected"
                task["reason"] = "Plan rejected by owner"
            else:
                task["approved_version"] = version
                task["status"] = "queued"
                task["phase"] = "prepare"
                task["pending"] = None
            task["history"].append(
                {"kind": f"plan_{action}", "version": version, "owner": owner}
            )
        elif action in {"reply", "supplement"}:
            if not text.strip():
                raise ValueError("An answer or supplement is required")
            task["supplements"].append(text)
            task["pending"] = None
            if action == "supplement" or task["phase"] not in {"research", "design"}:
                # Core changes create a new plan; existing run evidence stays intact.
                task["phase"] = "research"
                task["approved_version"] = None
                task["plan"] = {}
                task["plan_version"] += 1
                task["cursor"] = {}
            task["status"] = "queued"
        elif action == "continue":
            if task["status"] in {"waiting_confirmation", "waiting_user"}:
                raise ValueError("The current question or plan still needs a reply")
            task["status"] = "queued"
            task["reason"] = ""
            if pending and pending.get("native"):
                task["pending"] = None
        else:
            raise ValueError("Unsupported task action")
        self.store.save(task)
        if task["status"] == "queued":
            self.schedule(task_id)
        return task

    async def recover(self) -> None:
        """Pause interrupted model work and query submitted runs before recovery."""
        for task in self.store.list():
            if task.get("active_started_at") is not None:
                elapsed = max(0, time.time() - task["active_started_at"])
                task["usage"]["active_seconds"] += elapsed
                task["usage"]["recovery_wall_seconds"] = (
                    task["usage"].get("recovery_wall_seconds", 0) + elapsed
                )
                task["active_started_at"] = None
                self.store.save(task)
            platform = self.settings.get("platforms", {}).get(task["platform"], {})
            if task.get("instance_stop") and platform.get("type") == "autodl":
                try:
                    task["instance_state"] = await AutoDL(platform["autodl"]).invoke(
                        "status", platform["autodl"].get("instance")
                    )
                except Exception as exc:
                    task["instance_state"] = {
                        "status": "unknown",
                        "error": type(exc).__name__,
                    }
                self.store.save(task)
            if any(run["state"] not in FINAL_RUN_STATES for run in task["runs"]):
                self.monitor(task["id"])
            if task["status"] in TERMINAL:
                continue
            if task["status"] in {"active", "queued"} or (
                task.get("pending") and task["pending"].get("native")
            ):
                task["status"] = "paused"
                task["reason"] = (
                    "Bot restarted; continue to reconcile the saved session/run first"
                )
                self.store.save(task)
                await self.announce(task, self.status(task))

    def monitor(self, task_id: str) -> None:
        """Supervise existing processes independently of paused model work.

        Args:
            task_id: Task containing an existing submission.
        """
        if self.closed or task_id in self.monitors:
            return
        monitor = asyncio.create_task(self.watch(task_id))
        self.monitors[task_id] = monitor
        monitor.add_done_callback(lambda _: self.monitors.pop(task_id, None))

    async def watch(self, task_id: str) -> None:
        """Query, archive, and settle existing runs without calling a model.

        Args:
            task_id: Task to inspect; experiments are never submitted here.
        """
        failures = 0
        while not self.closed:
            await asyncio.sleep(1)
            if task_id in self.workers:
                continue
            task = self.store.load(task_id)
            outstanding = [
                run for run in task["runs"] if run["state"] not in FINAL_RUN_STATES
            ]
            if not outstanding:
                return
            try:
                platform = self.settings["platforms"][task["platform"]]
                executor = self.executor_factory(platform)
                for run in outstanding:
                    status = await executor.query(run["reference"])
                    if task_id in self.workers:
                        return
                    run.update(status)
                    self.store.save(task)
                    if run["state"] in FINAL_RUN_STATES:
                        entries = await executor.collect(
                            self.store.directory(task_id) / "workspace",
                            run["reference"],
                            task["plan"]["artifacts"]
                            if run["state"] == "succeeded"
                            else [],
                            task["limits"]["archive_bytes"],
                        )
                        if task_id in self.workers:
                            return
                        task["manifest"] = [
                            item
                            for item in task["manifest"]
                            if item.get("run_id") != run["id"]
                        ] + [{"run_id": run["id"], **entry} for entry in entries]
                        self.store.save(task)
                    elif (
                        run["state"] == "unknown"
                        or time.time() - run.get("submitted_at", time.time())
                        > task["limits"]["run_seconds"] + 20
                    ):
                        raise RuntimeError("Original run remains unconfirmed")
                failures = 0
                if all(run["state"] in FINAL_RUN_STATES for run in task["runs"]):
                    await self.settle(task, cancel=False)
                    await self.announce(task, self.status(task))
                    return
            except Exception as exc:
                if task_id in self.workers:
                    return
                failures += 1
                if failures > task["limits"]["network_retries"]:
                    task["reason"] = (
                        f"Process reconciliation blocked: {type(exc).__name__}; "
                        "existing submission retained, no resubmission"
                    )
                    await self.settle(task, cancel=False)
                    await self.announce(task, self.status(task))
                    return

    def status(self, task: dict) -> str:
        """Render a short query response including separate execution states.

        Args:
            task: Task record.

        Returns:
            Human-readable status and current artifact entry point.
        """
        run = task["runs"][-1] if task["runs"] else {}
        return (
            f"Research {task['id']}: {task['status']} / {task['phase']}\n"
            f"Plan v{task['plan_version']}, approved: {task['approved_version']}\n"
            f"{task['reason']}\n"
            f"Process: {run.get('state', 'not_submitted')}; "
            f"instance: {task.get('instance_state', 'not_managed')}\n"
            f"Usage: {json.dumps(task['usage'], ensure_ascii=False)}\n"
            f"Result: {json.dumps(task['result'], ensure_ascii=False)}\n"
            f"Artifacts: {self.store.directory(task['id'])}"
        )

    async def ask(
        self, task: dict, participant: str, phase: str, context: dict
    ) -> dict:
        """Run one bounded participant turn with synchronized constraints.

        Args:
            task: Current record.
            participant: Configured participant name.
            phase: Current role or stage.
            context: Minimal handoff and relevant evidence.

        Returns:
            Structured stage output.

        Raises:
            RuntimeError: If the response is invalid or a resource limit is reached.
        """
        if task["usage"]["model_calls"] >= task["limits"]["model_calls"]:
            raise RuntimeError("Model call limit reached")
        token_limit = task["limits"].get("tokens", 0)
        if token_limit and task["usage"]["tokens_actual"] >= token_limit:
            raise RuntimeError("Observed token threshold reached; hard cap unavailable")
        constraints = self.store.constraints(task)
        self.store.save(task)
        key = (task["id"], participant)
        adapter = self.live.get(key)

        async def emit(event: dict) -> None:
            """Checkpoint a native request and route it to the exact task owner."""
            if event["kind"] == "tool_evidence":
                task.setdefault("tool_evidence", []).append(
                    {"participant": participant, **event}
                )
                self.store.save(task)
            if event["kind"] in {"approval", "question"}:
                task["pending"] = {
                    "native": True,
                    "participant": participant,
                    "event": event,
                }
                task["status"] = "waiting_user"
                await self.announce(
                    task,
                    f"{participant} {event['kind']}: "
                    + json.dumps(event["details"], ensure_ascii=False)
                    + (
                        "\nNative execution grants are disabled; reply deny or stop."
                        if event["kind"] == "approval"
                        else ""
                    ),
                )

        if adapter is None:
            saved = task["participants"][participant]
            actual = self.settings["participants"][participant]
            if (actual.get("model"), actual.get("harness")) != (
                saved["model"],
                saved["harness"],
            ):
                raise RuntimeError(
                    "Participant settings changed; revise the task first"
                )
            adapter = self.harness_factory(
                actual, self.store.directory(task["id"]) / "workspace", emit
            )
            self.live[key] = adapter
            previous = task["sessions"].get(participant, {})
            info = await adapter.open(saved["model"], previous.get("reference"))
            # Keep discovery distinct from actual successful calls.
            task["sessions"][participant] = info
            self.store.save(task)
        prompt = (
            OUTPUT_CONTRACT
            + "\nPhase: "
            + phase
            + "\n"
            + json.dumps(
                {
                    "idea": task["idea"],
                    "supplements": task["supplements"],
                    "plan_version": task["plan_version"],
                    "plan": task["plan"],
                    "role": task["participants"][participant].get("role", phase),
                    "context": context,
                },
                ensure_ascii=False,
            )
            + "\n\n"
            + constraints
        )
        task["usage"]["model_calls"] += 1
        task["usage"]["unknown_calls"] += 1
        task["history"].append(
            {"kind": "model_call", "phase": phase, "participant": participant}
        )
        self.store.save(task)
        try:
            response = await adapter.turn(
                prompt,
                timeout=max(
                    1, task["limits"]["wall_seconds"] - task["usage"]["active_seconds"]
                ),
            )
        except Exception:
            task["sessions"][participant]["verified"] = False
            self.store.save(task)
            raise
        session = task["sessions"][participant]
        session["verified"] = True
        usage = response.get("usage")
        if usage is not None:
            if "actual" in usage:
                task["usage"]["unknown_calls"] -= 1
            task["usage"]["tokens_actual"] += usage.get("actual", 0)
            task["usage"]["tokens_cached"] += usage.get("cached", 0)
            task["usage"]["tokens_estimated"] += usage.get("estimated", 0)
        directory = self.store.directory(task["id"]) / "transcripts"
        directory.mkdir(exist_ok=True)
        transcript = (
            directory / f"{task['usage']['model_calls']:04d}-{participant}.json"
        )
        transcript.write_text(
            json.dumps(
                {
                    "phase": phase,
                    "participant": participant,
                    "constraints": task["agents"],
                    "response": response,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.store.save(task)
        text = response["text"].strip()
        if text.startswith("```") and text.endswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError("Harness response must be a JSON object")
        return result

    async def drive(self, task_id: str) -> None:
        """Advance a task until a durable user/resource gate or completion.

        Args:
            task_id: Task to execute.
        """
        async with self.semaphore:
            task = self.store.load(task_id)
            if task["status"] != "queued":
                return
            self.tasks[task_id] = task
            previous_constraints = task["agents"]
            self.store.constraints(task)
            if task["agents"] != previous_constraints:
                task["history"].append(
                    {
                        "kind": "constraints_synchronized",
                        "previous": previous_constraints,
                        "current": task["agents"],
                    }
                )
            task["status"] = "active"
            started = time.monotonic()
            base_seconds = task["usage"]["active_seconds"]
            task["active_started_at"] = time.time()
            self.store.save(task)
            try:
                remaining = task["limits"]["wall_seconds"] - base_seconds
                if remaining <= 0:
                    raise RuntimeError("Task active-time limit reached")
                await asyncio.wait_for(self.advance(task), remaining)
            except asyncio.CancelledError:
                task["status"] = "paused"
                task["reason"] = (
                    "Worker interrupted; reconcile the saved run before continuing"
                )
                self.store.save(task)
                raise
            except Exception as exc:
                task["status"] = "paused"
                task["reason"] = f"{type(exc).__name__}: {exc}"
                self.store.save(task)
                await self.settle(task, cancel=False)
                await self.announce(task, self.status(task))
            finally:
                task["usage"]["active_seconds"] = (
                    base_seconds + time.monotonic() - started
                )
                task["active_started_at"] = None
                for key in list(self.live):
                    if key[0] == task_id:
                        adapter = self.live.pop(key)
                        with contextlib.suppress(Exception):
                            await adapter.cancel()
                        with contextlib.suppress(Exception):
                            await adapter.close()
                self.store.save(task)
                self.tasks.pop(task_id, None)
                if any(run["state"] not in FINAL_RUN_STATES for run in task["runs"]):
                    self.monitor(task_id)

    async def advance(self, task: dict) -> None:
        """Execute the linear research workflow with explicit durable checkpoints.

        Args:
            task: Active task record.
        """
        while task["status"] == "active":
            phase = task["phase"]
            if phase in {"research", "design"}:
                participants = task["assignments"][phase]
                rounds = (
                    1 if phase == "research" else task["limits"]["discussion_rounds"]
                )
                cursor = task["cursor"]
                if cursor.get("phase") != phase:
                    cursor = {"phase": phase, "round": 0, "index": 0, "opinions": []}
                    task["cursor"] = cursor
                while cursor["round"] < rounds:
                    while cursor["index"] < len(participants):
                        name = participants[cursor["index"]]
                        output = await self.ask(
                            task,
                            name,
                            phase,
                            {
                                "round": cursor["round"] + 1,
                                "handoff": task["handoff"],
                                "new_opinions": cursor["opinions"][
                                    -len(participants) :
                                ],
                                "instruction": "Resolve new evidence and disagreements; avoid repeating history",
                            },
                        )
                        task["discussion"].append(
                            {
                                "phase": phase,
                                "round": cursor["round"] + 1,
                                "participant": name,
                                **output,
                            }
                        )
                        if output.get("question"):
                            task["status"] = "waiting_user"
                            task["pending"] = {
                                "participant": name,
                                "question": output["question"],
                            }
                            await self.announce(task, output["question"])
                            return
                        cursor["opinions"].append(
                            {
                                "participant": name,
                                "summary": output.get("summary", ""),
                                "evidence": output.get("evidence", []),
                                "disagreements": output.get("disagreements", []),
                            }
                        )
                        cursor["index"] += 1
                        self.store.save(task)
                    cursor["round"] += 1
                    cursor["index"] = 0
                    self.store.save(task)
                task["handoff"] = json.dumps(cursor["opinions"], ensure_ascii=False)
                if phase == "design":
                    summary = await self.ask(
                        task,
                        task["assignments"]["summary"][0],
                        "design synthesis",
                        {"opinions": cursor["opinions"], "research": task["handoff"]},
                    )
                    if summary.get("question"):
                        task["status"] = "waiting_user"
                        task["pending"] = {
                            "participant": task["assignments"]["summary"][0],
                            "question": summary["question"],
                        }
                        await self.announce(task, summary["question"])
                        return
                    plan = summary.get("plan", {})
                    if any(
                        not plan.get(key) and key != "artifacts" for key in PLAN_FIELDS
                    ):
                        raise ValueError(
                            "Plan lacks a required experiment agreement field"
                        )
                    if "artifacts" not in plan or not isinstance(
                        plan["artifacts"], list
                    ):
                        raise ValueError("Plan must specify its evidence files")
                    for name in plan["artifacts"]:
                        if (
                            not isinstance(name, str)
                            or PurePosixPath(name).is_absolute()
                            or ".." in PurePosixPath(name).parts
                            or "\\" in name
                            or ":" in name
                        ):
                            raise ValueError(
                                "Plan evidence files must use relative paths"
                            )
                    commands = plan["commands"]
                    if not isinstance(commands, list) or not commands:
                        raise ValueError("Plan commands must be a nonempty argv list")
                    if any(
                        not isinstance(argv, list)
                        or not argv
                        or any(
                            not isinstance(value, str) or "\x00" in value
                            for value in argv
                        )
                        for argv in commands
                    ):
                        raise ValueError(
                            "Plan commands must contain literal argv strings"
                        )
                    task["plan_version"] += 1
                    task["approved_version"] = None
                    task["plan"] = plan
                    task["history"].append(
                        {
                            "kind": "plan_proposed",
                            "version": task["plan_version"],
                            "plan": plan,
                        }
                    )
                    task["status"] = "waiting_confirmation"
                    task["reason"] = json.dumps(
                        summary.get("disagreements", []), ensure_ascii=False
                    )
                    await self.announce(
                        task,
                        f"Review plan v{task['plan_version']} for {task['id']}:\n"
                        + json.dumps(plan, ensure_ascii=False)
                        + "\nUse approve/reject with this exact version. No reply means wait.",
                    )
                    return
            elif phase == "prepare":
                if task["approved_version"] != task["plan_version"]:
                    raise RuntimeError("Current plan version has not been approved")
                platform = self.settings["platforms"][task["platform"]]
                executor = self.executor_factory(platform)
                if platform["type"] == "autodl":
                    manager = AutoDL(platform["autodl"])
                    task["instance_state"] = await manager.invoke(
                        "status", platform["autodl"].get("instance")
                    )
                    config = platform["autodl"]
                    if config.get("start_on_prepare") and task["instance_state"][
                        "status"
                    ] != config.get("running_status", "running"):
                        if task.get("instance_start"):
                            raise RuntimeError(
                                "AutoDL start unconfirmed; query before retrying"
                            )
                        task["instance_start"] = "submitting"
                        self.store.save(task)
                        await manager.invoke("start", config.get("instance"))
                        task["instance_start"] = "requested"
                        self.store.save(task)
                        for _ in range(10):
                            task["instance_state"] = await manager.invoke(
                                "status", config.get("instance")
                            )
                            if task["instance_state"]["status"] == config.get(
                                "running_status", "running"
                            ):
                                break
                            await asyncio.sleep(1)
                        else:
                            raise RuntimeError(
                                "AutoDL instance has not reached its running status"
                            )
                task["preflight"] = await executor.preflight()
            elif phase == "implement":
                for name in task["assignments"]["implement"]:
                    output = await self.ask(
                        task, name, phase, {"handoff": task["handoff"]}
                    )
                    while True:
                        try:
                            self.write_files(task, output.get("files", {}))
                            break
                        except SyntaxError as exc:
                            if (
                                task["usage"]["repair_rounds"]
                                >= task["limits"]["repair_rounds"]
                            ):
                                raise RuntimeError(
                                    "Model repair round limit reached"
                                ) from exc
                            task["usage"]["repair_rounds"] += 1
                            self.store.save(task)
                            output = await self.ask(
                                task,
                                name,
                                "repair",
                                {
                                    "diagnostic": f"Python syntax check failed: {exc}",
                                    "files": output.get("files", {}),
                                },
                            )
                    task["handoff"] = output.get("summary", "")
            elif phase == "run":
                await self.execute(task)
                if task["status"] != "active":
                    return
            elif phase == "analyze":
                for name in task["assignments"]["analyze"]:
                    output = await self.ask(
                        task,
                        name,
                        phase,
                        {
                            "runs": task["runs"],
                            "manifest": task["manifest"],
                            "evidence": task.get("evidence", {}),
                            "handoff": task["handoff"],
                        },
                    )
                    task["result"] = output
                    task["handoff"] = output.get("summary", "")
                if task["result"].get("verdict") not in {
                    "improved",
                    "no_improvement",
                    "insufficient_evidence",
                    "blocked",
                }:
                    raise ValueError(
                        "Analysis must report an explicit supported verdict"
                    )
                summary = await self.ask(
                    task,
                    task["assignments"]["summary"][0],
                    "final summary",
                    {
                        "result": task["result"],
                        "runs": task["runs"],
                        "usage": task["usage"],
                    },
                )
                task["result"]["delivery"] = summary
            elif phase == "archive":
                platform = self.settings["platforms"][task["platform"]]
                executor = self.executor_factory(platform)
                workspace = self.store.directory(task["id"]) / "workspace"
                for run in task["runs"]:
                    if run["state"] not in FINAL_RUN_STATES:
                        continue
                    entries = await executor.collect(
                        workspace,
                        run["reference"],
                        task["plan"]["artifacts"]
                        if run["state"] == "succeeded"
                        else [],
                        task["limits"]["archive_bytes"],
                    )
                    task["manifest"] = [
                        item
                        for item in task["manifest"]
                        if item.get("run_id") != run["id"]
                    ] + [{"run_id": run["id"], **entry} for entry in entries]
                    self.store.save(task)
                await self.settle(task, cancel=False)
                if any(item["status"] != "downloaded" for item in task["manifest"]):
                    task["status"] = "paused"
                    task["reason"] = (
                        "Required evidence is not fully archived; see manifest"
                    )
                else:
                    task["phase"] = "completed"
                    task["status"] = "completed"
                    task["reason"] = (
                        "Experiment finished; no automatic expansion or tuning"
                    )
                await self.announce(task, self.status(task))
                return
            task["phase"] = PHASES[PHASES.index(phase) + 1]
            task["cursor"] = {}
            self.store.save(task)

    def write_files(self, task: dict, files: dict) -> None:
        """Apply structured implementation files only within the task workspace.

        Args:
            task: Owning task.
            files: Relative path to UTF-8 file content.

        Raises:
            ValueError: If a write escapes the workspace or modifies AGENTS.
        """
        workspace = self.store.directory(task["id"]) / "workspace"
        if not isinstance(files, dict):
            raise ValueError("Implementation files must be an object")
        validated = []
        for name, content in files.items():
            relative = PurePosixPath(name)
            path = workspace / name
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or "\\" in name
                or ":" in name
                or any(part.lower() == "agents.md" for part in relative.parts)
                or ".runs" in relative.parts
                or path.is_symlink()
                or not path.resolve().is_relative_to(workspace.resolve())
                or not isinstance(content, str)
            ):
                raise ValueError(
                    "Implementation write violates workspace/AGENTS boundary"
                )
            if path.suffix == ".py":
                compile(content, str(relative), "exec")
            validated.append((path, content))
        for path, content in validated:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        task["code"] = {
            str(path.relative_to(workspace)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in workspace.rglob("*")
            if path.is_file() and not path.is_symlink() and ".runs" not in path.parts
        }
        self.store.save(task)

    async def execute(self, task: dict) -> None:
        """Reconcile existing submissions, execute approved argv, and repair finitely.

        Args:
            task: Task with an explicitly approved experiment agreement.
        """
        if task["approved_version"] != task["plan_version"]:
            raise RuntimeError(
                "Experiment execution requires the current plan approval"
            )
        platform = self.settings["platforms"][task["platform"]]
        executor = self.executor_factory(platform)
        workspace = self.store.directory(task["id"]) / "workspace"
        for index, argv in enumerate(task["plan"]["commands"]):
            while True:
                previous = [
                    run
                    for run in task["runs"]
                    if run["command_index"] == index
                    and run["plan_version"] == task["plan_version"]
                ]
                run = previous[-1] if previous else None
                if run is None or run.get("repair_pending") is True:
                    run_id = uuid.uuid4().hex
                    directory = (
                        str(Path(workspace / ".runs" / run_id))
                        if platform["type"] == "local"
                        else str(
                            PurePosixPath(platform.get("ssh", platform)["root"])
                            / run_id
                        )
                    )
                    run = {
                        "id": run_id,
                        "reference": {
                            "id": run_id,
                            "directory": directory,
                            "source_files": list(task.get("code", {})),
                        },
                        "plan_version": task["plan_version"],
                        "command_index": index,
                        "argv": argv,
                        "code": task.get("code", {}),
                        "constraints": task["agents"],
                        "state": "submitting",
                        "submitted_at": time.time(),
                    }
                    task["runs"].append(run)
                    self.store.save(task)
                    run["reference"] = await executor.submit(
                        workspace, run_id, argv, task["limits"]["run_seconds"]
                    )
                    self.store.save(task)
                attempts = 0
                while True:
                    try:
                        status = await executor.query(run["reference"])
                    except (OSError, RuntimeError, TimeoutError):
                        if attempts >= task["limits"]["network_retries"]:
                            raise RuntimeError(
                                "Run query unavailable; saved submission retained"
                            ) from None
                        attempts += 1
                        await asyncio.sleep(min(attempts, 3))
                        continue
                    run.update(status)
                    self.store.save(task)
                    if run["state"] in FINAL_RUN_STATES:
                        break
                    if run["state"] == "unknown":
                        raise RuntimeError(
                            "Run launch is ambiguous; inspect original run before continuing"
                        )
                    await asyncio.sleep(0.2)
                manifest = await executor.collect(
                    workspace,
                    run["reference"],
                    task["plan"]["artifacts"] if run["state"] == "succeeded" else [],
                    task["limits"]["archive_bytes"],
                )
                task["manifest"] = [
                    item for item in task["manifest"] if item.get("run_id") != run["id"]
                ] + [{"run_id": run["id"], **item} for item in manifest]
                evidence = {}
                for item in manifest:
                    if item["status"] == "downloaded" and item.get("local"):
                        path = Path(item["local"])
                        if path.suffix in {".json", ".log", ".txt"}:
                            evidence[item["path"]] = path.read_text(
                                encoding="utf-8", errors="replace"
                            )[-12000:]
                task["evidence"] = evidence
                self.store.save(task)
                if run["state"] == "succeeded":
                    break
                if run["state"] in {"cancelled", "timed_out"}:
                    raise RuntimeError(
                        f"Experiment process {run['state']}; no automatic expansion"
                    )
                if task["usage"]["repair_rounds"] >= task["limits"]["repair_rounds"]:
                    raise RuntimeError("Model repair round limit reached")
                signature = hashlib.sha256(
                    json.dumps(evidence, sort_keys=True).encode()
                ).hexdigest()
                if any(
                    attempt.get("failure_signature") == signature
                    for attempt in previous[:-1]
                ):
                    raise RuntimeError("Same failure recurred without new evidence")
                run["failure_signature"] = signature
                task["usage"]["repair_rounds"] += 1
                self.store.save(task)
                output = await self.ask(
                    task,
                    task["assignments"]["implement"][0],
                    "repair",
                    {"diagnostic": evidence, "previous_attempts": task["runs"][-2:]},
                )
                self.write_files(task, output.get("files", {}))
                run["repair_pending"] = True
                self.store.save(task)

    async def settle(self, task: dict, cancel: bool) -> None:
        """Track process cleanup and independently apply authorized instance policy.

        Args:
            task: Current persisted task.
            cancel: Whether the owner requested process cancellation.
        """
        platform = self.settings.get("platforms", {}).get(task["platform"], {})
        if task["runs"]:
            try:
                executor = self.executor_factory(platform)
                for run in task["runs"]:
                    if cancel and run["state"] not in FINAL_RUN_STATES:
                        await executor.cancel(run["reference"])
                        run["state"] = "cancel_requested"
            except Exception as exc:
                task["cleanup_error"] = type(exc).__name__
        if platform.get("type") == "autodl":
            try:
                manager = AutoDL(platform["autodl"])
                archive_ok = bool(task["manifest"]) and all(
                    item["status"] == "downloaded" for item in task["manifest"]
                )
                policy = platform["autodl"].get("stop_policy", "leave")
                should_stop = policy == "always" or (
                    policy == "after_archive" and archive_ok
                )
                if should_stop and task.get("instance_stop"):
                    task["instance_state"] = await manager.invoke(
                        "status", platform["autodl"].get("instance")
                    )
                else:
                    if should_stop:
                        task["instance_stop"] = "submitting"
                        self.store.save(task)
                    task["instance_state"] = await manager.finish(archive_ok)
                    if should_stop:
                        task["instance_stop"] = "requested"
            except Exception as exc:
                task["instance_state"] = {
                    "status": "unknown",
                    "error": type(exc).__name__,
                }
        self.store.save(task)

    async def close(self) -> None:
        """Checkpoint interrupted workers while detached supervisors keep deadlines."""
        self.closed = True
        workers = [*self.workers.values(), *self.monitors.values()]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
