"""Local Codex app-server and DeepSeek Harness ACP sessions.

Protocol references:
https://developers.openai.com/codex/app-server
https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/acp/acp
"""

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

EventSink = Callable[[dict], Awaitable[None]]


class RpcProcess:
    """Multiplex newline-delimited JSON-RPC without shell command interpolation.

    Args:
        command: Explicit executable and arguments.
        cwd: Local task workspace.
        request_handler: Handler for server requests and notifications.
    """

    def __init__(self, command: list[str], cwd: Path, request_handler: EventSink):
        self.command = command
        self.cwd = cwd
        self.handler = request_handler
        self.process = None
        self.reader = None
        self.stderr_reader = None
        self.pending = {}
        self.sequence = 0
        self.incoming = set()
        self.stderr = bytearray()

    async def start(self) -> None:
        """Start the transport and drain both output streams."""
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self.read())
        self.stderr_reader = asyncio.create_task(self.drain())

    async def drain(self) -> None:
        """Retain bounded local diagnostics while preventing pipe deadlocks."""
        while chunk := await self.process.stderr.read(8192):
            self.stderr.extend(chunk)
            del self.stderr[:-65536]

    async def read(self) -> None:
        """Dispatch responses immediately even while a permission reply is pending."""
        error = RuntimeError("Harness transport closed")
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" in message:
                    operation = asyncio.create_task(self.handler(message))
                    self.incoming.add(operation)
                    operation.add_done_callback(self.incoming.discard)
                elif message.get("id") in self.pending:
                    future = self.pending.pop(message["id"])
                    if not future.done():
                        if "error" in message:
                            future.set_exception(
                                RuntimeError(
                                    f"Harness RPC error: {message['error'].get('code')}"
                                )
                            )
                        else:
                            future.set_result(message.get("result", {}))
        except (ValueError, OSError, asyncio.LimitOverrunError) as exc:
            error = RuntimeError(f"Harness transport failed: {type(exc).__name__}")
        else:
            error = RuntimeError("Harness transport closed")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()

    async def send(self, message: dict) -> None:
        """Write a single protocol message.

        Args:
            message: JSON-RPC request, response, or notification.
        """
        self.process.stdin.write(
            (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await self.process.stdin.drain()

    async def call(self, method: str, params: dict, timeout: float | None = 90) -> dict:
        """Request a response with an optional transport deadline.

        Args:
            method: Protocol method name.
            params: Method-specific arguments.
            timeout: Maximum response wait in seconds, or None for no ceiling.

        Returns:
            Protocol result.
        """
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def close(self) -> None:
        """Close only this plugin-owned transport and fail pending requests."""
        if self.process is None:
            return
        if self.process.stdin:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except asyncio.TimeoutError:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        for operation in [self.reader, self.stderr_reader, *self.incoming]:
            if operation:
                operation.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await operation


class CodexHarness:
    """Maintain a real Codex app-server thread with explicit model selection.

    Args:
        config: Command and optional model provider settings.
        workspace: Absolute private task workspace.
        emit: Plugin event sink, including interactive requests.
    """

    kind = "codex"

    def __init__(self, config: dict, workspace: Path, emit: EventSink):
        self.config = config
        self.workspace = workspace.resolve()
        self.emit = emit
        self.rpc = RpcProcess(
            config.get("command", ["codex", "app-server"]), self.workspace, self.receive
        )
        self.session_id = None
        self.turn_id = None
        self.completion = None
        self.turn_outputs = {}
        self.finished_turns = {}
        self.usage = None
        self.models = []
        self.capabilities = {
            "interactive": True,
            "resume": True,
            "cancel": True,
            "approval": True,
            "native_questions": True,
            "search": "unverified",
            "usage": "optional",
            "token_hard_limit": False,
        }

    async def open(self, model: str, reference: str | None = None) -> dict:
        """Initialize, discover models, and start or resume the selected thread.

        Args:
            model: Exact configured model identifier.
            reference: Existing durable thread identifier.

        Returns:
            Session reference and discovered model metadata.

        Raises:
            ValueError: If no explicit model was supplied.
        """
        if not model:
            raise ValueError("An explicit Codex model is required")
        await self.rpc.start()
        await self.rpc.call(
            "initialize",
            {
                "clientInfo": {"name": "astrbot_research", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False},
            },
        )
        await self.rpc.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        configuration = await self.rpc.call("config/read", {"includeLayers": False})
        mcp_servers = configuration.get("config", {}).get("mcp_servers", {})
        cursor = None
        while True:
            response = await self.rpc.call(
                "model/list", {"cursor": cursor, "limit": 100}
            )
            self.models.extend(response.get("data", []))
            cursor = response.get("nextCursor")
            if not cursor:
                break
        params = {
            "model": model,
            "cwd": str(self.workspace),
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "sandbox": "read-only",
            "config": {
                "mcp_servers": {name: {"enabled": False} for name in mcp_servers},
                "sandbox_workspace_write.network_access": False,
                "web_search": self.config.get("web_search", "disabled"),
                "features.shell_tool": False,
                "features.code_mode": False,
                "features.multi_agent": False,
                "features.apps": False,
                "features.plugins": False,
                "features.hooks": False,
                "features.computer_use": False,
                "features.browser_use": False,
            },
        }
        if self.config.get("model_provider"):
            params["modelProvider"] = self.config["model_provider"]
        if reference:
            params["threadId"] = reference
        response = await self.rpc.call(
            "thread/resume" if reference else "thread/start", params
        )
        self.session_id = response["thread"]["id"]
        self.model = model
        return {
            "reference": self.session_id,
            "configured": model,
            "discovered": self.models,
            "verified": False,
            "capabilities": self.capabilities,
        }

    async def turn(self, prompt: str, timeout: float | None) -> dict:
        """Continue the current session and await its correlated completion.

        Args:
            prompt: Current-stage context and unchanged user constraints.
            timeout: Maximum turn duration, including user wait.

        Returns:
            Final assistant text and optional measured usage.
        """
        self.turn_outputs = {}
        self.finished_turns = {}
        self.usage = None
        self.turn_id = None
        self.completion = asyncio.get_running_loop().create_future()
        response = await self.rpc.call(
            "turn/start",
            {
                "threadId": self.session_id,
                "model": self.model,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
            },
        )
        self.turn_id = response["turn"]["id"]
        if self.turn_id in self.finished_turns and not self.completion.done():
            self.completion.set_result(self.finished_turns[self.turn_id])
        result = await asyncio.wait_for(self.completion, timeout)
        if result.get("status") != "completed":
            raise RuntimeError(f"Codex turn ended: {result.get('status')}")
        return {
            "text": "\n".join(self.turn_outputs.get(self.turn_id, [])),
            "usage": self.usage,
        }

    async def receive(self, message: dict) -> None:
        """Translate native events and expose approvals without auto-granting.

        Args:
            message: App-server message.
        """
        method = message["method"]
        params = message.get("params", {})
        if params.get("threadId") and params["threadId"] != self.session_id:
            return
        if "id" in message:
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/tool/requestUserInput",
            }:
                await self.emit(
                    {
                        "kind": "question"
                        if method.endswith("requestUserInput")
                        else "approval",
                        "request_id": message["id"],
                        "method": method,
                        "details": params,
                    }
                )
            else:
                await self.rpc.send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "Unsupported client request",
                        },
                    }
                )
        elif method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                self.turn_outputs.setdefault(params.get("turnId"), []).append(
                    item.get("text", "")
                )
            elif item.get("type") == "webSearch":
                await self.emit({"kind": "tool_evidence", "details": item})
        elif method == "thread/tokenUsage/updated":
            measured = params.get("tokenUsage", {}).get("last")
            if measured:
                self.usage = {
                    "actual": measured.get("totalTokens", 0),
                    "cached": measured.get("cachedInputTokens", 0),
                }
        elif method == "turn/completed" and self.completion:
            self.finished_turns[params["turn"]["id"]] = params["turn"]
            if params["turn"]["id"] == self.turn_id and not self.completion.done():
                self.completion.set_result(params["turn"])
        elif method == "error":
            await self.emit({"kind": "diagnostic", "details": params})

    async def answer(self, request: dict, answer: str) -> None:
        """Return one owner-authorized reply to the exact server request.

        Args:
            request: Saved event identifying the request and questions.
            answer: Owner reply; only the exact word allow grants a tool request.
        """
        if request["kind"] == "approval":
            result = {"decision": "accept" if answer == "allow" else "decline"}
        else:
            result = {
                "answers": {
                    question["id"]: {"answers": [answer]}
                    for question in request["details"].get("questions", [])
                }
            }
        await self.rpc.send(
            {"jsonrpc": "2.0", "id": request["request_id"], "result": result}
        )

    async def cancel(self) -> None:
        """Interrupt the active turn without deleting the durable thread."""
        if self.session_id and self.turn_id:
            await self.rpc.call(
                "turn/interrupt",
                {"threadId": self.session_id, "turnId": self.turn_id},
                timeout=10,
            )

    async def close(self) -> None:
        """Release the plugin-owned transport."""
        await self.rpc.close()


class DeepSeekHarness:
    """Drive the installed official DSH ACP profile, independently of Codex.

    Args:
        config: Explicit ACP launcher command.
        workspace: Absolute task workspace.
        emit: Interactive event sink.
    """

    kind = "deepseek"

    def __init__(self, config: dict, workspace: Path, emit: EventSink):
        self.config = config
        self.workspace = workspace.resolve()
        self.emit = emit
        self.rpc = RpcProcess(
            [
                *config.get("command", ["dsh", "--profile", "acp"]),
                "--patch",
                str(Path(__file__).with_name("dsh-research.patch.yml").resolve()),
            ],
            self.workspace,
            self.receive,
        )
        self.session_id = None
        self.text = []
        self.usage = None
        self.capabilities = {
            "interactive": True,
            "resume": False,
            "cancel": True,
            "approval": True,
            "native_questions": False,
            "search": "unverified",
            "usage": "unknown",
            "token_hard_limit": False,
        }

    async def open(self, model: str, reference: str | None = None) -> dict:
        """Negotiate ACP, discover opaque model choices, and select exactly one.

        Args:
            model: Exact advertised ACP model value, never a guessed alias.
            reference: Optional persisted ACP session ID.

        Returns:
            Session and configured/discovered/verified model states.

        Raises:
            ValueError: If the configured model is not advertised.
            RuntimeError: If session resume is unsupported.
        """
        await self.rpc.start()
        response = await self.rpc.call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "astrbot_research", "version": "0.1.0"},
                "clientCapabilities": {},
            },
        )
        caps = response.get("agentCapabilities", {})
        self.capabilities["resume"] = "resume" in caps.get("sessionCapabilities", {})
        if reference and not self.capabilities["resume"]:
            raise RuntimeError("This DSH ACP version does not advertise session resume")
        params = {"cwd": str(self.workspace), "mcpServers": []}
        if reference:
            params["sessionId"] = reference
        session = await self.rpc.call(
            "session/resume" if reference else "session/new", params
        )
        self.session_id = session.get("sessionId", reference)
        options = session.get("configOptions", [])
        models = next((o for o in options if o.get("id") == "model"), {})
        choices = []
        for option in models.get("options", []):
            choices.extend(option.get("options", [option]))
        if not model or model not in {o["value"] for o in choices}:
            raise ValueError("Configured DSH model is absent from its live ACP catalog")
        await self.rpc.call(
            "session/set_config_option",
            {"sessionId": self.session_id, "configId": "model", "value": model},
        )
        return {
            "reference": self.session_id,
            "configured": model,
            "discovered": choices,
            "verified": False,
            "capabilities": self.capabilities,
        }

    async def turn(self, prompt: str, timeout: float | None) -> dict:
        """Submit another prompt to the same ACP session.

        Args:
            prompt: Stage input including the applicable user AGENTS text.
            timeout: Maximum response duration.

        Returns:
            Committed output and unknown or available usage.
        """
        self.text = []
        response = await self.rpc.call(
            "session/prompt",
            {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": prompt}],
            },
            timeout=timeout,
        )
        if response.get("stopReason") != "end_turn":
            raise RuntimeError(f"DSH prompt ended: {response.get('stopReason')}")
        return {"text": "".join(self.text), "usage": self.usage}

    async def receive(self, message: dict) -> None:
        """Forward ACP approvals and collect only committed assistant text.

        Args:
            message: Incoming ACP JSON-RPC message.
        """
        method = message["method"]
        params = message.get("params", {})
        if params.get("sessionId") and params["sessionId"] != self.session_id:
            return
        if "id" in message:
            if method == "session/request_permission":
                await self.emit(
                    {
                        "kind": "approval",
                        "method": method,
                        "request_id": message["id"],
                        "details": params,
                    }
                )
            else:
                await self.rpc.send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "Unsupported client request",
                        },
                    }
                )
        elif method == "session/update":
            update = params.get("update", {})
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    self.text.append(content["text"])
            elif update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
                await self.emit({"kind": "tool_evidence", "details": update})
            elif update.get("sessionUpdate") == "usage_update":
                # ACP occupancy is not billable per-call usage.
                self.capabilities["context_occupancy"] = update.get("used")

    async def answer(self, request: dict, answer: str) -> None:
        """Map an explicit owner approval to an advertised one-shot choice.

        Args:
            request: Exact pending permission event.
            answer: Only allow selects an allow_once option.
        """
        options = request["details"].get("options", [])
        kind = "allow_once" if answer == "allow" else "reject_once"
        option = next((o for o in options if o["kind"] == kind), None)
        outcome = (
            {"outcome": "selected", "optionId": option["optionId"]}
            if option
            else {"outcome": "cancelled"}
        )
        await self.rpc.send(
            {
                "jsonrpc": "2.0",
                "id": request["request_id"],
                "result": {"outcome": outcome},
            }
        )

    async def cancel(self) -> None:
        """Cancel the current ACP activity without deleting its session."""
        if self.session_id:
            await self.rpc.send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": self.session_id},
                }
            )

    async def close(self) -> None:
        """Dispose this client's process and flush session persistence."""
        await self.rpc.close()


def make_harness(config: dict, workspace: Path, emit: EventSink):
    """Select only production adapters; test doubles are dependency-injected.

    Args:
        config: Participant settings with an explicit harness kind.
        workspace: Local working directory.
        emit: Workflow event callback.

    Returns:
        A supported real harness adapter.

    Raises:
        ValueError: If the harness kind is unsupported.
    """
    kind = config.get("harness")
    if kind == "codex":
        return CodexHarness(config, workspace, emit)
    if kind == "deepseek":
        return DeepSeekHarness(config, workspace, emit)
    raise ValueError("Only codex and deepseek are production harnesses")
