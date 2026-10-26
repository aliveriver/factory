"""AstrBot message entry point; all research goes directly to local harnesses."""

import asyncio
import json
import secrets
from pathlib import Path

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.web import error_response, json_response, request
from astrbot.core.star.filter.command import GreedyStr

from .configuration import read_configuration
from .execution import PROTECTED_HOST, SSHExecutor
from .harness import discover_harness_models
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
        self.config = config
        self.discovery_cache = {}
        settings, self.allowed = read_configuration(config)
        # This checkout exposes astrbot_path through StarTools, rather than the
        # older path_utils module named in the repository development guidance.
        self.store = Store(star.StarTools.get_data_dir("astrbot_plugin_research"))
        self.workflow = Workflow(self.store, settings, self.deliver)
        context.register_web_api(
            "/astrbot_plugin_research/discover/ssh",
            self.discover_ssh,
            ["POST"],
            "Read a configured SSH Python environment without changing the host",
        )
        context.register_web_api(
            "/astrbot_plugin_research/discover/ssh-config",
            self.discover_ssh_config,
            ["GET"],
            "Read local SSH config aliases without connecting to a host",
        )
        context.register_web_api(
            "/astrbot_plugin_research/discover/harness",
            self.discover_harness,
            ["POST"],
            "Discover models from a local Codex or DeepSeek Harness",
        )
        context.register_web_api(
            "/astrbot_plugin_research/configure/ssh",
            self.configure_ssh,
            ["POST"],
            "Save SSH connection and discovered environment fields",
        )
        context.register_web_api(
            "/astrbot_plugin_research/configure/harness",
            self.configure_harness,
            ["POST"],
            "Save a discovered local Harness member",
        )
        context.register_web_api(
            "/astrbot_plugin_research/config",
            self.get_page_config,
            ["GET"],
            "Read safe values for the research setup page",
        )

    async def get_page_config(self):
        """Return non-secret current values for the setup page."""
        participants = []
        for item in self.config.get("participants", []):
            if not isinstance(item, dict):
                continue
            participants.append(
                {
                    key: item.get(key, "")
                    for key in (
                        "__template_key",
                        "name",
                        "model",
                        "provider",
                        "role",
                        "stages",
                        "order",
                        "program",
                        "arguments",
                        "web_search",
                    )
                }
            )
        ssh = self.config.get("ssh", {})
        safe_ssh = {
            key: ssh.get(key, "")
            for key in ("host", "port", "user", "identity_file", "root", "python")
        }
        return json_response(
            {
                "config_mode": self.config.get("config_mode", "auto"),
                "participants": participants,
                "ssh": safe_ssh,
            }
        )

    async def discover_ssh(self):
        """Inspect a user-selected SSH Python environment with fixed read-only code."""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("SSH 设置必须是对象")
        config = {
            key: payload.get(key, default)
            for key, default in (
                ("host", ""),
                ("port", 22),
                ("user", ""),
                ("identity_file", ""),
                ("root", "/tmp/astrbot-research"),
                ("python", "python3"),
            )
        }
        if (
            not isinstance(config["host"], str)
            or not isinstance(config["user"], str)
            or not isinstance(config["identity_file"], str)
            or not isinstance(config["root"], str)
            or not isinstance(config["python"], str)
            or type(config["port"]) is not int
            or not 1 <= config["port"] <= 65535
            or not config["host"].strip()
            or not config["user"].strip()
            or not config["python"].strip()
            or "\x00" in config["python"]
        ):
            return error_response("SSH 地址、端口、用户和 Python 设置格式不正确")
        config["allow_execute"] = False
        if config["host"] == PROTECTED_HOST:
            return error_response("共享服务器禁止通过此探测接口访问", status_code=403)
        try:
            executor = SSHExecutor(config, require_execute=False)
            result = await asyncio.wait_for(executor.inspect_environment(), 45)
        except (
            OSError,
            PermissionError,
            ValueError,
            RuntimeError,
            TimeoutError,
        ) as exc:
            return error_response(f"SSH 环境读取失败：{type(exc).__name__}：{exc}")
        return json_response(
            {
                "environment": result,
                "capabilities": (
                    f"Python: {result.get('python', '')}\n"
                    f"Platform: {result.get('platform', '')}\n"
                    f"Machine: {result.get('machine', '')}\n"
                    "Dependencies discovered by importlib.metadata."
                ),
            }
        )

    async def discover_ssh_config(self):
        """Read safe connection fields from the local OpenSSH config file.

        Returns:
            Host aliases and non-secret connection fields. Wildcard entries and
            private key contents are never returned.
        """
        config_path = Path.home() / ".ssh" / "config"
        if not config_path.is_file():
            return json_response({"path": str(config_path), "hosts": []})
        hosts = []
        current = []
        values = {}

        def append_current() -> None:
            if not current or not values.get("hostname"):
                return
            for alias in current:
                if any(item["alias"] == alias for item in hosts):
                    continue
                hosts.append(
                    {
                        "alias": alias,
                        "host": values["hostname"],
                        "port": values.get("port", 22),
                        "user": values.get("user", ""),
                        "identity_file": values.get("identityfile", ""),
                    }
                )

        try:
            for raw_line in config_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                key, value = parts[0].lower(), parts[1].strip()
                if key == "host":
                    append_current()
                    current = [
                        item
                        for item in value.split()
                        if "*" not in item and "?" not in item
                    ]
                    values = {}
                elif current and key in {"hostname", "port", "user", "identityfile"}:
                    values[key] = value
            append_current()
        except (OSError, UnicodeDecodeError) as exc:
            return error_response(f"读取本机 SSH 配置失败：{type(exc).__name__}：{exc}")
        return json_response({"path": str(config_path), "hosts": hosts})

    async def discover_harness(self):
        """Discover local Harness model choices without creating a research task."""
        payload = await request.json(default={})
        if not isinstance(payload, dict) or payload.get("kind") not in {
            "codex",
            "deepseek",
        }:
            return error_response("Harness 类型必须是 codex 或 deepseek")
        kind = payload["kind"]
        program = payload.get("program", "")
        arguments = payload.get("arguments", [])
        if not isinstance(program, str) or not isinstance(arguments, list):
            return error_response("Harness 程序和参数格式不正确")
        default_args = ["app-server"] if kind == "codex" else ["--profile", "acp"]
        command = [program.strip() or ("codex" if kind == "codex" else "dsh")]
        command.extend(
            argument
            for argument in (arguments or default_args)
            if isinstance(argument, str) and "\x00" not in argument
        )
        if len(command) != 1 + len(arguments or default_args):
            return error_response("Harness 参数必须是文本")
        try:
            models = await asyncio.wait_for(
                discover_harness_models(
                    kind,
                    {
                        "harness": kind,
                        "command": command,
                        "web_search": payload.get("web_search", "disabled"),
                    },
                ),
                45,
            )
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            return error_response(f"Harness 模型读取失败：{type(exc).__name__}：{exc}")
        discovery_id = secrets.token_urlsafe(24)
        self.discovery_cache[discovery_id] = {
            "kind": kind,
            "command": command,
            "models": models,
        }
        return json_response(
            {
                "kind": kind,
                "command": command,
                "discovery_id": discovery_id,
                "models": models,
            }
        )

    async def configure_ssh(self):
        """Persist selected SSH fields while leaving execution permissions unchanged."""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("SSH 设置必须是对象")
        if payload.get("host") == PROTECTED_HOST:
            return error_response("共享服务器不能保存为实验平台", status_code=403)
        if any(
            key in payload
            and (
                not isinstance(payload[key], str)
                if key != "port"
                else type(payload[key]) is not int
            )
            for key in ("host", "user", "identity_file", "root", "python", "port")
        ):
            return error_response("SSH 设置字段类型不正确")
        if "port" in payload and (
            type(payload["port"]) is not int or not 1 <= payload["port"] <= 65535
        ):
            return error_response("SSH 端口必须是 1 到 65535 的整数")
        if any(
            key in payload and (not payload[key].strip() or "\x00" in payload[key])
            for key in ("host", "user", "root", "python")
        ):
            return error_response(
                "SSH 地址、用户、目录和 Python 命令不能为空或含非法字符"
            )
        form = self.config.setdefault("ssh", {})
        for key in ("host", "port", "user", "identity_file", "root", "python"):
            if key in payload:
                form[key] = payload[key]
        if isinstance(payload.get("capabilities"), str):
            form["capabilities"] = payload["capabilities"]
        await asyncio.to_thread(self.config.save_config)
        return json_response({"saved": True, "note": "请重新加载插件使执行设置生效"})

    async def configure_harness(self):
        """Persist one discovered Harness member in the visual configuration."""
        payload = await request.json(default={})
        if not isinstance(payload, dict) or payload.get("kind") not in {
            "codex",
            "deepseek",
        }:
            return error_response("Harness 类型必须是 codex 或 deepseek")
        name = payload.get("name", "")
        model = payload.get("model", "")
        discovery_id = payload.get("discovery_id", "")
        discovery = self.discovery_cache.get(discovery_id)
        models = discovery.get("models", []) if discovery else []
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(model, str)
            or not model.strip()
            or not discovery
            or discovery.get("kind") != payload.get("kind")
            or not isinstance(models, list)
            or not any(
                isinstance(item, dict) and item.get("value") == model for item in models
            )
        ):
            return error_response("请选择探测结果中的模型后再保存")
        if self.config.get("config_mode", "auto") == "json" or (
            self.config.get("config_mode", "auto") == "auto"
            and self.config.get("settings_json", "{}").strip() not in {"", "{}"}
        ):
            return error_response("当前使用旧版 JSON；请切换到可视化表单后保存 Harness")
        members = self.config.setdefault("participants", [])
        if not isinstance(name, str) or not name.strip():
            return error_response("成员名称不能为空")
        stages = payload.get(
            "stages", ["research", "design", "implement", "analyze", "summary"]
        )
        arguments = payload.get("arguments", [])
        if (
            not isinstance(stages, list)
            or any(
                stage not in {"research", "design", "implement", "analyze", "summary"}
                for stage in stages
            )
            or not isinstance(arguments, list)
            or any(
                not isinstance(argument, str) or "\x00" in argument
                for argument in arguments
            )
        ):
            return error_response("成员阶段或启动参数格式不正确")
        if not isinstance(members, list):
            return error_response("现有成员配置格式不正确")
        members = [
            item
            for item in members
            if not isinstance(item, dict) or item.get("name") != name.strip()
        ]
        kind = payload["kind"]
        member = {
            "__template_key": kind,
            "name": name.strip(),
            "model": model,
            "provider": payload.get("provider", "") if kind == "deepseek" else "",
            "role": payload.get("role", ""),
            "stages": stages,
            "order": payload.get("order", 10),
            "program": payload.get("program", ""),
            "arguments": arguments,
        }
        if kind == "codex":
            member["web_search"] = payload.get("web_search", "disabled")
        members.append(member)
        self.config["participants"] = members
        await asyncio.to_thread(self.config.save_config)
        self.discovery_cache.pop(discovery_id, None)
        return json_response({"saved": True, "note": "请重新加载插件使成员设置生效"})

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
