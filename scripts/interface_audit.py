"""Read-only behavioral probes. Differences are findings, NOT conformance passes.

Run after bootstrap_followup_reference.py. No network services or credentials used.
"""

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import subprocess

from bootstrap_followup_reference import verify_sources
from pi_python import (
    Agent,
    AgentContext,
    AgentLoopConfig,
    AssistantMessage,
    CancelToken,
    Event,
    Hooks,
    ModelEvent,
    ScriptedProvider,
    UserMessage,
    run_agent_loop,
    run_agent_loop_continue,
)
from pi_python.lowlevel import AgentEventStream
from pi_python.stream import checked_events
from pi_python import core

ROOT = Path(__file__).resolve().parents[1]


async def observe_python():
    observed = {}
    for key, reason, timestamp in [("old_message", "stop", 1), ("new_deferred", "deferred", None)]:

        async def source():
            message = AssistantMessage([], stop_reason=reason)
            if timestamp is not None:
                message.timestamp = timestamp
            yield ModelEvent.done(message)

        events = [e async for e in checked_events(source(), AssistantMessage([]))]
        observed[key] = {"timed": events[-1].message.duration_ms is not None}
    token = CancelToken()
    token.cancel()
    events = []
    result = await run_agent_loop(
        [UserMessage("go")],
        AgentContext(),
        AgentLoopConfig(provider=ScriptedProvider([AssistantMessage.text("ok")])),
        events.append,
        token,
    )
    observed["pre_cancelled"] = {
        "events": [e.type for e in events],
        "roles": [m.role for m in result],
    }
    try:
        await run_agent_loop_continue(
            AgentContext(
                messages=[UserMessage("go"), AssistantMessage([], "error", error="failed")]
            ),
            AgentLoopConfig(provider=ScriptedProvider([AssistantMessage.text("ok")])),
        )
        observed["continue_failed_assistant"] = {"accepted": True}
    except Exception as exc:
        observed["continue_failed_assistant"] = {
            "accepted": False,
            "error_type": type(exc).__name__,
        }
    agent = Agent(
        provider=ScriptedProvider([AssistantMessage.text("ok")]),
        hooks=Hooks(finish_turn=lambda *_: {"action": "end"}),
    )
    result = await agent.prompt("go")
    observed["finish_object"] = {"accepted": result.status == "completed"}
    agent = Agent(provider=ScriptedProvider([AssistantMessage.text("ok")]))
    events = []
    agent.subscribe(events.append)
    await agent.prompt("go")
    event = next(e for e in events if e.type == "agent_end")
    observed["agent_end"] = {"keys": sorted(asdict(event)), "data_keys": sorted(event.data)}

    async def runner(emit):
        for i in range(200):
            await emit(Event("probe", "audit", 0, i + 1))
        return []

    stream = AgentEventStream(runner)
    iterator = stream.__aiter__()
    await anext(iterator)
    try:
        await asyncio.wait_for(stream.result(), 0.1)
        remaining = [event async for event in iterator]
        observed["partial_stream_result"] = {"completed": True, "remaining_events": len(remaining)}
    except TimeoutError:
        observed["partial_stream_result"] = {
            "completed": False,
            "queue_full": stream.queue.full(),
            "producer_pending": not stream.task.done(),
            "observation_seconds": 0.1,
        }
    finally:
        await stream.aclose()
    return observed


async def main():
    verify_sources()
    upstream = subprocess.run(
        ["node", "--import", "./reference/register-followup.mjs", "./reference/interface-audit.ts"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    official = json.loads(upstream.stdout)
    python = await observe_python()
    canonical = {
        key: python[key] for key in ("old_message", "new_deferred", "partial_stream_result")
    }
    token = CancelToken()
    token.cancel()
    seen = []
    result = await core.run_agent_loop(
        [UserMessage("go")],
        core.AgentContext(),
        core.AgentLoopConfig(provider=ScriptedProvider([AssistantMessage.text("ok")])),
        seen.append,
        token,
    )
    canonical["pre_cancelled"] = {
        "events": [e["type"] for e in seen],
        "roles": [m.role for m in result],
    }
    try:
        await core.run_agent_loop_continue(
            core.AgentContext([UserMessage("go"), AssistantMessage([], "error", error="failed")]),
            core.AgentLoopConfig(provider=ScriptedProvider([])),
        )
        canonical["continue_failed_assistant"] = {"accepted": True}
    except Exception:
        canonical["continue_failed_assistant"] = {"accepted": False}
    agent = core.Agent(
        provider=ScriptedProvider([AssistantMessage.text("ok")]),
        finish_turn=lambda c, s: {"action": "end"},
    )
    seen = []
    agent.subscribe(lambda e, s: seen.append(e))
    await agent.prompt("go")
    canonical["finish_object"] = {"accepted": agent.state.error_message is None}
    canonical["agent_end"] = {"keys": sorted(seen[-1])}
    report = {
        "upstream_commit": "4a080252f06caf63a525d7873c5d94d3c23887e0",
        "scope": "7 个定向探针；core 入口须全部一致，研究入口的有意差异单独记录；不是完整接口兼容性证明",
        "findings": [
            {
                "case": key,
                "python": value,
                "core": canonical[key],
                "upstream": official[key],
                "core_matches": canonical[key] == official[key],
            }
            for key, value in python.items()
        ],
    }
    path = ROOT / "compat/results/interface-audit.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not all(item["core_matches"] for item in report["findings"]):
        raise SystemExit("Core interface probe mismatch")


if __name__ == "__main__":
    asyncio.run(main())
