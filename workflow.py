"""Research orchestration with durable gates and bounded model work."""

import asyncio
import contextlib
import hashlib
import json
import os
import platform as local_platform
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

import psutil

from .allocation import Allocation, resource_spec, task_platform
from .execution import FINAL_RUN_STATES, PROTECTED_HOST, AutoDL, make_executor
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
When context.round_limit is null, discussion has no round ceiling. Include
discussion_complete=true only when your material issues are resolved; stop after
all participants agree to finish.
For platform selection: assessments (one per candidate, in the given order),
each with platform, suitability (suitable, unsuitable, unknown), reason.
Assess actual experiment requirements against known platform capabilities and
constraints. Never guess missing GPU, dependency, dataset or server capabilities.
An unknown higher-priority platform requires clarification before escalation.
Do not mark a capable platform unsuitable solely because execution is not yet
authorized. Unconfigured or protected candidates cannot be suitable.
For design synthesis: summary, disagreements, plan containing baseline, change,
data, metric, scale, decision_rule, resource_policy, commands (lists of argv),
artifacts (relative files needed to verify results).
If allocation_profiles are provided, plan must include resources with profile
(one exact allowed profile name) and reason. Select the smallest suitable approved
profile for the experiment; never invent GPU, image, price, region or quota values.
Do not invent a baseline, dataset, measurement, external capability or success.
For implement/repair: summary, files (relative path to complete UTF-8 content).
Implement the approved plan only; never change its comparison, metric or scope.
For analyze: verdict (improved, no_improvement, insufficient_evidence, blocked),
summary, baseline_metrics, modified_metrics, limitations, key_disagreements.
No improvement is a valid result, not a program failure or a reason to tune.
Do not execute commands or modify files yourself. Return changes in files.
Never use SSH, scp, platform CLIs, credentials or remote tools. The plugin alone
owns experiment execution, resource limits, transport, polling and archives.
All harnesses always run on this local computer, including for SSH and AutoDL
experiments. Never install or start any harness on an experiment server.
Proposed commands must run in the foreground; do not detach background jobs.
User AGENTS constraints below apply unchanged to all participants.
"""


def evidence_saved(entries: list[dict]) -> bool:
    """Check the local bytes before reusing evidence after instance shutdown.

    Args:
        entries: Archive manifest entries for one or more runs.

    Returns:
        Whether every entry has a local file matching its recorded checksum.
    """
    if not entries:
        return False
    for entry in entries:
        try:
            if entry["status"] != "downloaded" or not entry.get("local"):
                return False
            digest = hashlib.sha256()
            with Path(entry["local"]).open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != entry.get("sha256"):
                return False
        except OSError:
            return False
    return True


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
        platform = self.settings.get("platform", "local")
        order = self.settings.get("platform_order", ["local", "ssh", "autodl"])
        if platform == "auto" and (
            not order
            or len(set(order)) != len(order)
            or any(name not in self.settings.get("platforms", {}) for name in order)
        ):
            raise ValueError(
                "Automatic execution needs a valid platform priority order"
            )
        task = self.store.create(
            owner,
            address,
            idea,
            {
                "participants": safe,
                "assignments": assignments,
                "platform": platform,
                "platform_order": order if platform == "auto" else [],
                "platform_agents_file": self.settings.get("platforms", {})
                .get(platform, {})
                .get("agents_file", ""),
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
            monitor = self.monitors.get(task_id)
            if monitor:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
                self.monitors.pop(task_id, None)
            task = self.store.load(task_id, owner)
            task["status"] = "cancelled"
            task["reason"] = (
                "Stopped by owner; process and instance states are separate"
            )
            self.store.save(task)
            await self.settle(task, cancel=True)
            await self.announce(task, self.status(task))
            if task.get("allocation") and not task["allocation"].get("stopped"):
                self.monitor(task_id)
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
            if (
                action == "supplement"
                or task["phase"] not in {"research", "design", "select_platform"}
            ) and any(run["state"] not in FINAL_RUN_STATES for run in task["runs"]):
                raise ValueError(
                    "Original run is unresolved; reconcile or stop it before revising the plan"
                )
            task["supplements"].append(text)
            task["pending"] = None
            if action == "supplement" or task["phase"] not in {
                "research",
                "design",
                "select_platform",
            }:
                # Core changes create a new plan; existing run evidence stays intact.
                task["phase"] = "research"
                task["approved_version"] = None
                task["plan"] = {}
                task["plan_version"] += 1
                task["cursor"] = {}
                if (
                    task.get("platform_order")
                    and not task["runs"]
                    and not task.get("allocation")
                ):
                    task["platform"] = "auto"
                    task["platform_agents_file"] = ""
                    task.pop("platform_selection", None)
                    task["sessions"] = {}
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
        # A user transition owns the checkpoint; stop a concurrent reconciliation
        # before saving or scheduling a worker that may restart the same instance.
        monitor = self.monitors.get(task_id)
        if monitor:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
            self.monitors.pop(task_id, None)
        self.store.save(task)
        if task["status"] == "queued":
            self.schedule(task_id)
        elif task.get("allocation") and not task["allocation"].get("stopped"):
            self.monitor(task_id)
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
            if task.get("allocation"):
                self.monitor(task["id"])
            elif task.get("instance_stop") and platform.get("type") == "autodl":
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
            if not outstanding or task.get("allocation", {}).get("shutdown_started_at"):
                if task.get("allocation") and not task["allocation"].get("stopped"):
                    await self.settle(task, cancel=False)
                    if task["allocation"].get("stopped"):
                        if (
                            task["phase"] == "archive"
                            and task.get("archive_complete")
                            and task["status"] not in TERMINAL
                        ):
                            task.update(
                                phase="completed",
                                status="completed",
                                reason="Evidence archived; allocated instance shutdown confirmed",
                            )
                            await self.announce(task, self.status(task))
                        return
                    timeout = (
                        self.settings.get("platforms", {})
                        .get(task["platform"], {})
                        .get("autodl", {})
                        .get("shutdown_seconds", 120)
                    )
                    if type(timeout) is not int or not 1 <= timeout <= 3600:
                        timeout = 120
                    if (
                        time.time()
                        - task["allocation"].get(
                            "shutdown_started_at",
                            task["allocation"].get("created_at", 0),
                        )
                        > timeout
                    ):
                        task["reason"] = (
                            "Allocated instance shutdown is unconfirmed; inspect allocation ID/name before further operations"
                        )
                        self.store.save(task)
                        await self.announce(task, self.status(task))
                        return
                    continue
                return
            try:
                platform = task_platform(
                    self.settings["platforms"][task["platform"]], task
                )
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
                    elif run["state"] == "unknown" or (
                        task["limits"]["run_seconds"] is not None
                        and time.time() - run.get("submitted_at", time.time())
                        > task["limits"]["run_seconds"] + 20
                    ):
                        raise RuntimeError("Original run remains unconfirmed")
                failures = 0
                if all(run["state"] in FINAL_RUN_STATES for run in task["runs"]):
                    await self.settle(task, cancel=False)
                    await self.announce(task, self.status(task))
                    if not task.get("allocation") or task["allocation"].get("stopped"):
                        return
            except Exception as exc:
                if task_id in self.workers:
                    return
                failures += 1
                if (
                    task["limits"]["network_retries"] is not None
                    and failures > task["limits"]["network_retries"]
                ):
                    task["reason"] = (
                        f"Process reconciliation blocked: {type(exc).__name__}; "
                        "existing submission retained, no resubmission"
                    )
                    await self.settle(task, cancel=False)
                    await self.announce(task, self.status(task))
                    if task.get("allocation") and not task["allocation"].get("stopped"):
                        continue
                    return

    def status(self, task: dict) -> str:
        """Render a short query response including separate execution states.

        Args:
            task: Task record.

        Returns:
            Human-readable status and current artifact entry point.
        """
        run = task["runs"][-1] if task["runs"] else {}
        allocation = task.get("allocation", {})
        return (
            f"Research {task['id']}: {task['status']} / {task['phase']}\n"
            f"Plan v{task['plan_version']}, approved: {task['approved_version']}\n"
            f"Execution platform: {task['platform']}; harnesses: local\n"
            f"{task['reason']}\n"
            f"Process: {run.get('state', 'not_submitted')}; "
            f"instance: {task.get('instance_state', 'not_managed')}\n"
            f"Allocation: {allocation.get('instance', allocation.get('name', 'not_allocated'))}; "
            f"state: {allocation.get('state', 'not_managed')}\n"
            f"Usage: {json.dumps(task['usage'], ensure_ascii=False)}\n"
            f"Result: {json.dumps(task['result'], ensure_ascii=False)}\n"
            f"Artifacts: {self.store.directory(task['id'])}"
        )

    async def select_platform(self, task: dict) -> None:
        """Choose the first suitable configured platform before plan approval.

        Args:
            task: Unallocated task with an ordered platform candidate snapshot.

        Raises:
            ValueError: If the assessment is incomplete or invents a destination.
            RuntimeError: If a previous allocation or run would be moved.
        """
        if task["runs"] or task.get("allocation"):
            raise RuntimeError("An existing run or allocation cannot change platforms")
        candidates = []
        for name in task["platform_order"]:
            config = self.settings["platforms"][name]
            kind = config.get("type")
            connection = config.get("ssh", config)
            cloud = config.get("autodl", {})
            forbidden = kind in {"ssh", "autodl"} and PROTECTED_HOST in json.dumps(
                connection
            )
            configured = (
                kind == "local"
                or (
                    kind == "ssh"
                    and all(connection.get(key) for key in ("host", "user", "root"))
                )
                or (
                    kind == "autodl"
                    and all(connection.get(key) for key in ("user", "root"))
                    and (
                        bool(cloud.get("resource_profiles"))
                        if cloud.get("mode") == "create"
                        else bool(cloud.get("instance") and connection.get("host"))
                    )
                )
            ) and not forbidden
            candidate_task = {
                **task,
                "platform_agents_file": config.get("agents_file", ""),
            }
            constraints = self.store.constraints(candidate_task)
            candidate = {
                "name": name,
                "type": kind,
                "configured": bool(configured),
                "protected": forbidden,
                "execution_authorized": (
                    config.get("allow_execute", False)
                    if kind == "local"
                    else connection.get("allow_execute", False)
                ),
                "capabilities": config.get("capabilities", ""),
                "constraints": constraints,
                "agents": candidate_task["agents"],
            }
            if kind == "local":
                candidate["observed"] = {
                    "os": local_platform.system(),
                    "python": local_platform.python_version(),
                    "logical_cpus": os.cpu_count(),
                    "memory_total_bytes": psutil.virtual_memory().total,
                    "gpu": "unknown unless specified in administrator capabilities",
                    "dependencies": "unknown unless specified in administrator capabilities",
                }
            if kind == "autodl" and cloud.get("mode") == "create":
                candidate["allocation_profiles"] = {
                    profile: {
                        key: value
                        for key, value in values.items()
                        if key
                        in {
                            "description",
                            "gpu_spec",
                            "image",
                            "cuda_min",
                            "gpus",
                            "disk_gb",
                            "region",
                        }
                    }
                    for profile, values in cloud.get("resource_profiles", {}).items()
                }
            candidates.append(candidate)
        output = await self.ask(
            task,
            task["assignments"]["summary"][0],
            "platform selection",
            {
                "candidates": candidates,
                "research": task["handoff"],
                "instruction": (
                    "Evaluate candidates in user priority order against this experiment. "
                    "Candidate constraints are prospective rules for that destination, "
                    "not simultaneous instructions for other destinations. "
                    "No remote probing, paid operations or experiment execution occurs "
                    "during selection. Unknown earlier capabilities need clarification."
                ),
            },
        )
        if output.get("question"):
            task["status"] = "waiting_user"
            task["pending"] = {
                "participant": task["assignments"]["summary"][0],
                "question": output["question"],
            }
            await self.announce(task, output["question"])
            return
        assessments = output.get("assessments")
        if (
            not isinstance(assessments, list)
            or [item.get("platform") for item in assessments if isinstance(item, dict)]
            != task["platform_order"]
            or len(assessments) != len(candidates)
        ):
            raise ValueError(
                "Platform assessment must cover every candidate in priority order"
            )
        for assessment, candidate in zip(assessments, candidates):
            if (
                assessment.get("suitability")
                not in {"suitable", "unsuitable", "unknown"}
                or not isinstance(assessment.get("reason"), str)
                or not assessment["reason"].strip()
            ):
                raise ValueError(
                    "Every platform assessment needs suitability and a reason"
                )
            if assessment["suitability"] == "suitable" and not candidate["configured"]:
                raise ValueError(
                    "An unconfigured or protected platform cannot be selected"
                )
        task["platform_selection"] = {
            "assessments": assessments,
            "candidates": [
                {key: value for key, value in candidate.items() if key != "constraints"}
                for candidate in candidates
            ],
        }
        chosen = None
        for assessment in assessments:
            if assessment["suitability"] == "unknown":
                question = f"请补充平台 {assessment['platform']} 的能力信息：{assessment['reason']}"
                break
            if assessment["suitability"] == "suitable":
                chosen = assessment["platform"]
                break
        else:
            question = "当前候选平台均无法满足实验需求，请补充资源或调整实验范围。"
        if chosen is None:
            task["status"] = "waiting_user"
            task["pending"] = {
                "participant": task["assignments"]["summary"][0],
                "question": question,
            }
            await self.announce(task, question)
            return
        task["platform"] = chosen
        task["platform_agents_file"] = self.settings["platforms"][chosen].get(
            "agents_file", ""
        )
        task["platform_selection"]["chosen"] = chosen
        # A different platform's rules must not inherit an old harness session.
        for key in list(self.live):
            if key[0] == task["id"]:
                adapter = self.live.pop(key)
                await adapter.close()
        task["sessions"] = {}
        self.store.constraints(task)
        task["history"].append({"kind": "platform_selected", "platform": chosen})
        self.store.save(task)

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
        if (
            task["limits"]["model_calls"] is not None
            and task["usage"]["model_calls"] >= task["limits"]["model_calls"]
        ):
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
                if task.get("allocation"):
                    deadline = time.monotonic() + self.settings["platforms"][
                        task["platform"]
                    ]["autodl"].get("shutdown_seconds", 120)
                    while True:
                        await self.settle(task, cancel=False)
                        if task["allocation"].get("stopped"):
                            break
                        if time.monotonic() >= deadline:
                            await self.announce(
                                task,
                                "Instance shutdown is unconfirmed; inspect the saved allocation.",
                            )
                            break
                        await asyncio.sleep(1)

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
        platform = self.settings.get("platforms", {}).get(task["platform"], {})
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
                    "execution_environment": {
                        "platform": task["platform"],
                        "type": platform.get("type"),
                        "capabilities": platform.get("capabilities", ""),
                        "python": (
                            platform.get("ssh", platform).get("python", "python3")
                            if platform.get("type") in {"ssh", "autodl"}
                            else sys.executable
                        ),
                        "harness_location": "local",
                    },
                    "allocation_profiles": (
                        self.settings.get("platforms", {})
                        .get(task["platform"], {})
                        .get("autodl", {})
                        .get("resource_profiles", {})
                        if self.settings.get("platforms", {})
                        .get(task["platform"], {})
                        .get("autodl", {})
                        .get("mode")
                        == "create"
                        else {}
                    ),
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
                timeout=(
                    None
                    if task["limits"]["wall_seconds"] is None
                    else max(
                        1,
                        task["limits"]["wall_seconds"]
                        - task["usage"]["active_seconds"],
                    )
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
            task["status"] = "active"
            started = time.monotonic()
            base_seconds = task["usage"]["active_seconds"]
            task["active_started_at"] = time.time()
            self.store.save(task)
            try:
                platform = self.settings.get("platforms", {}).get(task["platform"], {})
                task["platform_agents_file"] = platform.get("agents_file", "")
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
                self.store.save(task)
                remaining = (
                    None
                    if task["limits"]["wall_seconds"] is None
                    else task["limits"]["wall_seconds"] - base_seconds
                )
                if remaining is not None and remaining <= 0:
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
                if task.get("allocation") and task["status"] != "completed":
                    await self.settle(task, cancel=False)
                    if (
                        task["allocation"].get("stopped")
                        and task.get("archive_complete")
                        and task["phase"] == "archive"
                        and task["status"] == "paused"
                    ):
                        task.update(
                            phase="completed",
                            status="completed",
                            reason="Evidence archived; allocated instance shutdown confirmed",
                        )
                        await self.announce(task, self.status(task))
                if any(run["state"] not in FINAL_RUN_STATES for run in task["runs"]):
                    self.monitor(task_id)
                elif task.get("allocation") and not task["allocation"].get("stopped"):
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
                while not cursor.get("complete") and (
                    rounds is None or cursor["round"] < rounds
                ):
                    while cursor["index"] < len(participants):
                        name = participants[cursor["index"]]
                        output = await self.ask(
                            task,
                            name,
                            phase,
                            {
                                "round": cursor["round"] + 1,
                                "round_limit": rounds,
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
                                "discussion_complete": output.get("discussion_complete")
                                is True,
                            }
                        )
                        cursor["index"] += 1
                        self.store.save(task)
                    cursor["round"] += 1
                    cursor["index"] = 0
                    self.store.save(task)
                    if rounds is None and all(
                        item.get("discussion_complete")
                        and not item.get("disagreements")
                        for item in cursor["opinions"][-len(participants) :]
                    ):
                        cursor["complete"] = True
                        self.store.save(task)
                        break
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
                    plan["execution_platform"] = {
                        "name": task["platform"],
                        "selection": task.get("platform_selection", {}),
                        "agents": task["agents"],
                    }
                    platform = self.settings.get("platforms", {}).get(
                        task["platform"], {}
                    )
                    if platform.get("autodl", {}).get("mode") == "create":
                        spec = resource_spec(platform, plan)
                        plan["resources"] = {
                            "profile": plan["resources"]["profile"],
                            "reason": plan["resources"]["reason"],
                            "spec": spec,
                            "lifecycle": "create_after_approval_and_always_stop",
                        }
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
            elif phase == "select_platform":
                await self.select_platform(task)
                if task["status"] != "active":
                    return
                task["phase"] = "design"
                task["cursor"] = {}
                self.store.save(task)
                continue
            elif phase == "prepare":
                if task["approved_version"] != task["plan_version"]:
                    raise RuntimeError("Current plan version has not been approved")
                agreement = task["plan"].get("execution_platform")
                if agreement and (
                    agreement["name"] != task["platform"]
                    or agreement["agents"] != task["agents"]
                ):
                    raise RuntimeError(
                        "Execution platform constraints changed; supplement and approve a new plan"
                    )
                platform = self.settings["platforms"][task["platform"]]
                if (
                    task.get("allocation")
                    or platform.get("autodl", {}).get("mode") == "create"
                ):
                    platform = await Allocation(
                        platform, task, self.store.save
                    ).prepare()
                elif platform["type"] == "autodl":
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
                executor = self.executor_factory(platform)
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
                                task["limits"]["repair_rounds"] is not None
                                and task["usage"]["repair_rounds"]
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
                platform = task_platform(
                    self.settings["platforms"][task["platform"]], task
                )
                executor = self.executor_factory(platform)
                workspace = self.store.directory(task["id"]) / "workspace"
                for run in task["runs"]:
                    if run["state"] not in FINAL_RUN_STATES:
                        continue
                    saved = [
                        entry
                        for entry in task["manifest"]
                        if entry.get("run_id") == run["id"]
                    ]
                    if task.get("allocation") and evidence_saved(saved):
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
                task["archive_complete"] = bool(task["manifest"]) and all(
                    entry["status"] == "downloaded" for entry in task["manifest"]
                )
                if task.get("allocation"):
                    task["archive_complete"] = evidence_saved(task["manifest"])
                self.store.save(task)
                await self.settle(task, cancel=False)
                if any(item["status"] != "downloaded" for item in task["manifest"]) or (
                    task.get("allocation") and not task["archive_complete"]
                ):
                    task["status"] = "paused"
                    task["reason"] = (
                        "Required evidence is not fully archived; see manifest"
                    )
                elif task.get("allocation") and not task["allocation"].get("stopped"):
                    task["status"] = "paused"
                    task["reason"] = (
                        "Evidence archived; waiting for allocated instance shutdown confirmation"
                    )
                else:
                    task["phase"] = "completed"
                    task["status"] = "completed"
                    task["reason"] = (
                        "Experiment finished; no automatic expansion or tuning"
                    )
                await self.announce(task, self.status(task))
                return
            task["phase"] = (
                "select_platform"
                if phase == "research" and task["platform"] == "auto"
                else PHASES[PHASES.index(phase) + 1]
            )
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
        agreement = task["plan"].get("execution_platform")
        self.store.constraints(task)
        if agreement and (
            agreement["name"] != task["platform"]
            or agreement["agents"] != task["agents"]
        ):
            raise RuntimeError(
                "Execution platform constraints changed; supplement and approve a new plan"
            )
        platform = task_platform(self.settings["platforms"][task["platform"]], task)
        if task.get("allocation") and task["allocation"].get("stopped"):
            platform = await Allocation(
                self.settings["platforms"][task["platform"]], task, self.store.save
            ).prepare()
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
                        if (
                            task["limits"]["network_retries"] is not None
                            and attempts >= task["limits"]["network_retries"]
                        ):
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
                            with path.open("rb") as stream:
                                stream.seek(max(0, path.stat().st_size - 48000))
                                evidence[item["path"]] = stream.read(48000).decode(
                                    "utf-8", errors="replace"
                                )[-12000:]
                task["evidence"] = evidence
                self.store.save(task)
                if run["state"] == "succeeded":
                    break
                if run["state"] in {"cancelled", "timed_out", "interrupted"}:
                    raise RuntimeError(
                        f"Experiment process {run['state']}; no automatic expansion"
                    )
                if (
                    task["limits"]["repair_rounds"] is not None
                    and task["usage"]["repair_rounds"]
                    >= task["limits"]["repair_rounds"]
                ):
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
        try:
            platform = task_platform(
                self.settings.get("platforms", {}).get(task["platform"], {}), task
            )
        except ValueError:
            task["instance_state"] = {
                "status": "unknown",
                "error": "Restore the original AutoDL platform configuration",
            }
            self.store.save(task)
            return
        if task["runs"] and not (task.get("allocation") and task.get("instance_stop")):
            try:
                executor = self.executor_factory(platform)
                for run in task["runs"]:
                    if (cancel or task.get("allocation")) and run[
                        "state"
                    ] not in FINAL_RUN_STATES:
                        await executor.cancel(run["reference"])
                        run["state"] = "cancel_requested"
                        if task.get("allocation"):
                            deadline = time.monotonic() + 5
                            while time.monotonic() < deadline:
                                run.update(await executor.query(run["reference"]))
                                if run["state"] in FINAL_RUN_STATES:
                                    break
                                await asyncio.sleep(0.2)
                    if task.get("allocation") and run["state"] in FINAL_RUN_STATES:
                        saved = [
                            entry
                            for entry in task["manifest"]
                            if entry.get("run_id") == run["id"]
                        ]
                        if not evidence_saved(saved):
                            entries = await executor.collect(
                                self.store.directory(task["id"]) / "workspace",
                                run["reference"],
                                task["plan"]["artifacts"]
                                if run["state"] == "succeeded"
                                else [],
                                task["limits"]["archive_bytes"],
                            )
                            task["manifest"] = [
                                entry
                                for entry in task["manifest"]
                                if entry.get("run_id") != run["id"]
                            ] + [{"run_id": run["id"], **entry} for entry in entries]
            except Exception as exc:
                task["cleanup_error"] = type(exc).__name__
        if task.get("allocation"):
            try:
                await Allocation(platform, task, self.store.save).stop()
                if task["allocation"].get("stopped"):
                    for run in task["runs"]:
                        if run["state"] not in FINAL_RUN_STATES:
                            run.update(
                                state="interrupted",
                                exit_code=None,
                                reason="Instance shutdown confirmed; supervisor exit status unavailable",
                            )
            except Exception as exc:
                task["instance_state"] = {
                    "status": "unknown",
                    "error": type(exc).__name__,
                }
        elif platform.get("type") == "autodl":
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
