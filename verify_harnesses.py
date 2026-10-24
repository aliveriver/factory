"""Opt-in real harness acceptance; never run experiments or platform commands."""

import argparse
import asyncio
import json
from pathlib import Path

from .harness import make_harness
from .state import Store
from .workflow import Workflow


async def verify(settings: dict, output: Path) -> None:
    """Verify real collaboration and lifecycle using an explicit local fixture.

    Args:
        settings: Explicit production participant commands, models, and AGENTS path.
        output: Private local evidence directory.

    Raises:
        RuntimeError: If a required real capability fails.
    """
    output.mkdir(parents=True, exist_ok=True)
    settings = {
        **settings,
        "platform": "acceptance",
        "platforms": {"acceptance": {"type": "local", "allow_execute": False}},
        "limits": {**settings.get("limits", {}), "discussion_rounds": 1},
    }
    selected = settings.get("participants", {})
    if {item.get("harness") for item in selected.values()} != {"codex", "deepseek"}:
        raise ValueError("Select explicit real Codex and DeepSeek participants")
    messages = []

    async def notify(task, text):
        """Capture fixture notifications locally, without messaging other people."""
        messages.append(text)
        print(f"{task['status']} / {task['phase']}", flush=True)

    flow = Workflow(Store(output / "tasks"), settings, notify)
    evidence = {"kind": "REAL_HARNESS_ACCEPTANCE_WITH_CONTROLLED_INPUTS"}
    try:
        task = flow.create(
            "LOCAL_ACCEPTANCE_OWNER",
            "LOCAL_ACCEPTANCE_OUTBOX",
            "Integration acceptance fixture only, not a real research finding. "
            "Compare baseline identity(x)=x and modification identity(x)=x+0 "
            "on fixed integer inputs [0,1,2] and expected outputs [0,1,2]. "
            "The metric is missing: ask the user which metric, using question, "
            "and wait. After the reply, discuss fair comparison with both "
            "participants and synthesize a complete prospective plan, including "
            "a nonempty Python argv list and proposed metrics.json evidence. "
            "Do not invoke tools, run commands, write files, approve the plan, "
            "or claim any observed result. Listing proposed future commands is "
            "allowed; executing them is not authorized.",
        )
        evidence["task_id"] = task["id"]
        flow.schedule(task["id"])
        await flow.workers[task["id"]]
        await asyncio.sleep(0)
        task = flow.store.load(task["id"])
        if task["status"] != "waiting_user":
            raise RuntimeError("Real clarification did not reach its user gate")
        await flow.interact(
            task["id"],
            task["owner"],
            "reply",
            "The controlled fixture owner chooses exact-match rate against "
            "[0,1,2]. Propose commands for future approval, but execute nothing.",
        )
        await flow.workers[task["id"]]
        await asyncio.sleep(0)
        task = flow.store.load(task["id"])
        if task["status"] != "waiting_confirmation" or task["runs"]:
            raise RuntimeError(f"Real collaboration failed: {task['reason']}")
        evidence["collaboration"] = {
            "status": task["status"],
            "constraints": task["agents"],
            "participants": task["participants"],
            "discussion": task["discussion"],
            "plan": task["plan"],
            "usage": task["usage"],
            "runs": task["runs"],
        }
        evidence["lifecycle"] = {}
        for name, participant in selected.items():
            events = []

            async def emit(event):
                """Capture actual protocol events and decline tool escalations."""
                events.append(event)
                if event["kind"] == "approval":
                    await adapter.answer(event, "deny")

            workspace = flow.store.directory(task["id"]) / "workspace"
            adapter = make_harness(participant, workspace, emit)
            record = {"model": participant["model"]}
            evidence["lifecycle"][name] = record
            try:
                reference = task["sessions"][name]["reference"]
                info = await adapter.open(participant["model"], reference)
                record["resumed"] = info["reference"] == reference
                marker = task["id"][-12:]
                record["first"] = await adapter.turn(
                    f"Acceptance lifecycle fixture. Remember marker {marker}. "
                    "Ask the user in plain text which metric to use. No tools.",
                    120,
                )
                record["second"] = await adapter.turn(
                    "User answer: accuracy. Return the previous marker and "
                    "metric as JSON; do not invent values or invoke tools.",
                    120,
                )
                if marker not in record["second"]["text"]:
                    raise RuntimeError("Real session lost its previous input")
                adapter.turn_id = None
                active = asyncio.create_task(
                    adapter.turn(
                        "Cancellation fixture. No tools. Write the integers "
                        "from 1 to 100000, one per line, until interrupted.",
                        120,
                    )
                )
                if participant["harness"] == "codex":
                    for _ in range(100):
                        if adapter.turn_id or active.done():
                            break
                        await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(0.5)
                await adapter.cancel()
                try:
                    await asyncio.wait_for(active, 15)
                except RuntimeError as exc:
                    record["cancellation"] = str(exc)
                    if not any(
                        word in str(exc) for word in ("interrupted", "cancelled")
                    ):
                        raise
                else:
                    raise RuntimeError("Cancellation did not interrupt the active turn")
                record["continued"] = await adapter.turn(
                    "No tools. Return only our remembered marker.", 120
                )
                if marker not in record["continued"]["text"]:
                    raise RuntimeError("Session did not continue after cancellation")
                record["search"] = await adapter.turn(
                    "Acceptance search fixture: invoke web_search once to find "
                    "official Python documentation for pathlib.Path.is_relative_to. "
                    "Report the actual result or actual failure honestly. Do not "
                    "use commands, files, credentials, subagents or remote hosts.",
                    120,
                )
                if not any(event["kind"] == "tool_evidence" for event in events):
                    raise RuntimeError("No actual search tool event was observed")
                record["events"] = events
            finally:
                await adapter.close()
        evidence["passed"] = True
    except Exception as exc:
        evidence["passed"] = False
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        evidence["notifications"] = messages
        (output / "acceptance.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await flow.close()


def main() -> None:
    """Run only when explicitly invoked with a private settings file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(
        verify(
            json.loads(args.settings.read_text(encoding="utf-8")),
            args.output.resolve(),
        )
    )


if __name__ == "__main__":
    main()
