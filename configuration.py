"""Translate AstrBot's native configuration form into workflow settings."""

import json
from copy import deepcopy

from .allocation import resource_spec
from .state import DEFAULT_LIMITS

PHASES = {
    "research": "想法完善与检索",
    "design": "实验设计讨论",
    "implement": "编写实现",
    "analyze": "公平性检查与分析",
    "summary": "最终汇总",
}


def read_configuration(config: dict) -> tuple[dict, set[str]]:
    """Resolve configuration sources without merging incompatible permissions.

    Args:
        config: Host-populated schema values, including legacy settings.

    Returns:
        Workflow settings and canonical platform/user identity strings.

    Raises:
        ValueError: If a selected form value or legacy payload is invalid.
    """
    allowed = set(config.get("allowed_users", []))
    for user in config.get("users", []):
        platform, sender = user.get("platform_id", ""), user.get("sender_id", "")
        if not isinstance(platform, str) or not isinstance(sender, str):
            raise ValueError("使用权限：平台 ID 和用户 ID 必须是文本")
        if not platform.strip() or not sender.strip():
            raise ValueError("使用权限：请填写平台 ID 和用户 ID，或删除空白条目")
        allowed.add(
            json.dumps(
                [platform.strip(), sender.strip()],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    mode = config.get("config_mode", "auto")
    if mode not in {"auto", "visual", "json"}:
        raise ValueError("配置来源：请选择自动、可视化表单或旧版 JSON")
    if mode != "visual":
        try:
            legacy = json.loads(config.get("settings_json", "{}"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("旧版 JSON 无法解析；请修正或明确选择可视化表单") from exc
        if not isinstance(legacy, dict):
            raise ValueError("旧版 JSON 必须是对象")
        if mode == "json" or legacy:
            return legacy, allowed

    members = config.get("participants", [])
    participants, assignments = {}, {phase: [] for phase in PHASES}
    if not isinstance(members, list):
        raise ValueError("参与者必须使用成员卡片列表")
    for member in members:
        if type(member.get("order", 10)) is not int:
            raise ValueError("参与者：发言顺序必须是整数")
    for member in sorted(members, key=lambda item: item.get("order", 10)):
        name = member.get("name", "").strip()
        if not name or name in participants:
            raise ValueError("参与者：名称不能为空或重复")
        if any(character in name for character in "/\\\x00"):
            raise ValueError("参与者：名称不能包含路径分隔符或空字符")
        harness = member.get("__template_key")
        if harness not in {"codex", "deepseek"}:
            raise ValueError(f"参与者 {name}：请选择 Codex 或 DeepSeek 模板")
        model = member.get("model", "").strip()
        if not model:
            raise ValueError(f"参与者 {name}：请填写明确的模型标识")
        provider = member.get("provider", "").strip()
        if harness == "deepseek" and provider:
            model = json.dumps(
                [provider, model], ensure_ascii=False, separators=(",", ":")
            )
        arguments = member.get("arguments", [])
        if not isinstance(arguments, list) or any(
            not isinstance(arg, str) or "\x00" in arg for arg in arguments
        ):
            raise ValueError(f"参与者 {name}：启动参数应逐项填写文本")
        default_args = ["app-server"] if harness == "codex" else ["--profile", "acp"]
        participants[name] = {
            "harness": harness,
            "model": model,
            "command": [
                member.get("program", "").strip()
                or ("codex" if harness == "codex" else "dsh"),
                *(arguments or default_args),
            ],
            "role": member.get("role", ""),
        }
        if harness == "codex":
            search = member.get("web_search", "disabled")
            if search not in {"disabled", "cached", "live"}:
                raise ValueError(f"参与者 {name}：无效的联网搜索选项")
            participants[name]["web_search"] = search
            if provider:
                participants[name]["model_provider"] = provider
        phases = member.get("stages", [])
        if not isinstance(phases, list) or any(phase not in PHASES for phase in phases):
            raise ValueError(f"参与者 {name}：请选择有效的负责阶段")
        for phase in PHASES:
            if phase in phases:
                assignments[phase].append(name)
    if participants:
        for phase, names in assignments.items():
            if not names:
                raise ValueError(f"阶段分工：至少一位参与者需要负责“{PHASES[phase]}”")

    platform = config.get("platform", "local")
    if platform not in {"auto", "local", "ssh", "autodl"}:
        raise ValueError("执行平台：请选择自动、本机、SSH 或 AutoDL")
    routing = config.get("routing", {})
    order = [
        routing.get(key, default)
        for key, default in (("first", "local"), ("second", "ssh"), ("third", "autodl"))
    ]
    order = [name for name in order if name != "none"]
    if platform == "auto" and (
        not order
        or len(set(order)) != len(order)
        or any(name not in {"local", "ssh", "autodl"} for name in order)
    ):
        raise ValueError("平台优先顺序：至少选择一个平台，且不能重复")
    local = config.get("local", {})
    ssh = deepcopy(config.get("ssh", {}))
    cloud_form = deepcopy(config.get("autodl", {}))
    cloud_ssh = cloud_form.pop("ssh", {})
    profiles = cloud_form.pop("resource_profiles", [])
    cloud = cloud_form
    cloud["command"] = [cloud.pop("program", "") or "autodl"]
    cloud["resource_profiles"] = {}
    for profile in profiles:
        name = profile.get("name", "").strip()
        if not name or name in cloud["resource_profiles"]:
            raise ValueError("AutoDL 资源档位：名称不能为空或重复")
        cloud["resource_profiles"][name] = {
            key: value
            for key, value in profile.items()
            if key not in {"name", "__template_key"}
        }
    platforms = {
        "local": {
            "type": "local",
            "allow_execute": local.get("allow_execute") is True,
        },
        "ssh": {"type": "ssh", "ssh": ssh},
        "autodl": {"type": "autodl", "ssh": cloud_ssh, "autodl": cloud},
    }
    for name, form in (("local", local), ("ssh", ssh), ("autodl", cloud)):
        platforms[name]["agents_file"] = (
            form.pop("agents_file", "")
            if name != "local"
            else form.get("agents_file", "")
        )
        platforms[name]["capabilities"] = (
            form.pop("capabilities", "")
            if name != "local"
            else form.get("capabilities", "")
        )
    for connection in (ssh, cloud_ssh):
        connection["allow_execute"] = connection.get("allow_execute") is True
        connection["accept_new_host_keys"] = (
            connection.get("accept_new_host_keys") is True
        )
    for permission in (
        "allow_create",
        "allow_discovery",
        "allow_start",
        "allow_stop",
        "start_on_prepare",
    ):
        cloud[permission] = cloud.get(permission) is True
    if platform == "autodl":
        if cloud.get("mode", "existing") not in {"create", "existing"}:
            raise ValueError("AutoDL：请选择自动创建或使用已有实例")
        if cloud.get("kind", "pro") not in {"pro", "container"}:
            raise ValueError("AutoDL：请选择 Pro 或普通容器")
        if cloud.get("stop_policy", "leave") not in {
            "leave",
            "after_archive",
            "always",
        }:
            raise ValueError("AutoDL：请选择有效的已有实例关机策略")
        for key, label, default in (
            ("startup_seconds", "等待启动", 300),
            ("shutdown_seconds", "等待关机确认", 120),
        ):
            value = cloud.get(key, default)
            if type(value) is not int or not 1 <= value <= 3600:
                raise ValueError(f"AutoDL：{label}时间必须为 1–3600 秒")
        if cloud.get("mode") == "create":
            if cloud.get("kind", "pro") != "pro":
                raise ValueError("AutoDL：自动创建仅支持 Pro 实例")
            if not cloud["resource_profiles"]:
                raise ValueError("AutoDL：自动创建至少需要一个资源档位")
            for name in cloud["resource_profiles"]:
                try:
                    resource_spec(
                        platforms["autodl"],
                        {"resources": {"profile": name, "reason": "Form validation"}},
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"AutoDL 资源档位 {name}：请核对 GPU、镜像、CUDA 编码、"
                        "GPU 数量（1–4）、磁盘（0–500 GB）和地域编码"
                    ) from exc

    limit_form = config.get("limits", {})
    limits = {}
    for key, default in DEFAULT_LIMITS.items():
        if limit_form.get(f"unlimited_{key}") is True:
            limits[key] = None
            continue
        value = limit_form.get(key, default)
        if type(value) is not int or value < 0:
            raise ValueError(f"资源限制 {key}：请填写非负整数")
        limits[key] = value
    concurrency = config.get("concurrency", 1)
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("同时推进任务数：请填写至少为 1 的整数")
    return {
        "participants": participants,
        "assignments": assignments,
        "agents_file": config.get("agents_file", ""),
        "platform": platform,
        "platforms": platforms,
        "platform_order": order,
        "concurrency": concurrency,
        "limits": deepcopy(limits),
    }, allowed
