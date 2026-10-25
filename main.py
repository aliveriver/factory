"""AstrBot message entry point; all research goes directly to local harnesses."""

import json

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.star.filter.command import GreedyStr

from .configuration import read_configuration
from .state import Store
from .workflow import Workflow

HELP = (
    "research new <idea>\nresearch list\nresearch status <task>\n"
    "research reply <task> <answer>\nresearch supplement <task> <new information>\n"
    "research approve <task> <version>\nresearch reject <task> <version>\n"
    "research stop <task>\nresearch continue <task>\n"
    "research configure <task> <JSON: limits, assignments, participants>\n"
    "research artifacts <task>\nresearch plan <task>\nresearch models <task>"
)


class Main(star.Star):
    """Load an independent plugin using AstrBot's normal data and message paths.

    Args:
        context: Host plugin context.
        config: Host-managed plugin settings.
    """

    def __init__(self, context: star.Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        settings, self.allowed = read_configuration(config)
        # This checkout exposes astrbot_path through StarTools, rather than the
        # older path_utils module named in the repository development guidance.
        self.store = Store(star.StarTools.get_data_dir("astrbot_plugin_research"))
        self.workflow = Workflow(self.store, settings, self.deliver)

    async def initialize(self) -> None:
        """Expose persisted recovery checkpoints after plugin load."""
        await self.workflow.recover()

    async def deliver(self, task: dict, text: str) -> None:
        """Send a short notification to the original task message destination.

        Args:
            task: Record containing the return address.
            text: Summary or intervention request.
        """
        if len(text) > 12000:
            text = (
                text[:11500]
                + f"\nFull plan: research plan {task['id']}\n"
                + f"Full result: research status {task['id']}"
            )
        await self.context.send_message(task["address"], MessageChain().message(text))

    @filter.command("research")
    async def research(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """Handle research commands before the normal conversation LLM.

        Args:
            event: Authenticated platform message.
            args: Command action and remainder.
        """
        event.stop_event()
        event.should_call_llm(False)
        owner = json.dumps(
            [str(event.get_platform_id()), str(event.get_sender_id())],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if not event.is_admin() and owner not in self.allowed:
            yield event.plain_result("Research access is not configured for this user.")
            return
        try:
            action, _, remainder = str(args).strip().partition(" ")
            if action == "new":
                if not remainder.strip():
                    raise ValueError("Provide an idea after research new")
                task = self.workflow.create(owner, event.unified_msg_origin, remainder)
                self.workflow.schedule(task["id"])
                response = self.workflow.status(task)
            elif action == "list":
                response = (
                    "\n".join(
                        f"{task['id']} {task['status']} / {task['phase']}"
                        for task in self.store.list(owner)
                    )
                    or "No research tasks."
                )
            elif action in {"status", "artifacts", "plan", "models"}:
                task = self.store.load(remainder.strip(), owner)
                response = (
                    self.workflow.status(task)
                    if action == "status"
                    else json.dumps(
                        {
                            "artifacts": task["manifest"],
                            "plan": task["plan"],
                            "models": task["sessions"],
                        }[action],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif action == "configure":
                task_id, _, payload = remainder.partition(" ")
                task = self.store.load(task_id, owner)
                if task["status"] in {"completed", "cancelled", "rejected"}:
                    raise ValueError("This task has ended; submit a new idea")
                if task_id in self.workflow.workers:
                    raise ValueError("Stop active work before changing task settings")
                patch = json.loads(payload)
                if set(patch) - {"limits", "assignments", "participants"}:
                    raise ValueError(
                        "Only limits, assignments and participants can change"
                    )
                if "limits" in patch:
                    for key, value in patch["limits"].items():
                        if key not in task["limits"] or (
                            value is not None and (type(value) is not int or value < 0)
                        ):
                            raise ValueError("Unknown or invalid resource limit")
                    task["limits"].update(patch["limits"])
                if "participants" in patch:
                    selected = patch["participants"]
                    if not isinstance(selected, list) or any(
                        name not in self.workflow.settings.get("participants", {})
                        for name in selected
                    ):
                        raise ValueError(
                            "Select existing administrator-configured participants"
                        )
                    task["participants"] = {
                        name: {
                            key: value
                            for key, value in self.workflow.settings["participants"][
                                name
                            ].items()
                            if key in {"harness", "model", "role"}
                        }
                        for name in selected
                    }
                    task["sessions"] = {}
                if "assignments" in patch:
                    for stage, names in patch["assignments"].items():
                        if (
                            stage not in task["assignments"]
                            or not names
                            or any(name not in task["participants"] for name in names)
                        ):
                            raise ValueError("Invalid stage participant assignment")
                    task["assignments"].update(patch["assignments"])
                if any(
                    name not in task["participants"]
                    for names in task["assignments"].values()
                    for name in names
                ):
                    raise ValueError("Every assignment must use selected participants")
                if task["plan"] and set(patch) & {"participants", "assignments"}:
                    task["approved_version"] = None
                    task["plan_version"] += 1
                    task["status"] = "waiting_confirmation"
                self.store.save(task)
                response = self.workflow.status(task)
            elif action in {
                "reply",
                "supplement",
                "approve",
                "reject",
                "stop",
                "continue",
            }:
                task_id, _, text = remainder.partition(" ")
                version = int(text) if action in {"approve", "reject"} else None
                task = await self.workflow.interact(
                    task_id, owner, action, text=text, version=version
                )
                response = self.workflow.status(task)
            else:
                response = HELP
        except (ValueError, PermissionError, FileNotFoundError, KeyError) as exc:
            response = f"Research command rejected: {exc}"
        yield event.plain_result(response)

    async def terminate(self) -> None:
        """Checkpoint owned workers when the plugin unloads."""
        await self.workflow.close()
