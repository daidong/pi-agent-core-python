"""The ``subagent`` tool: delegate tasks to agents defined by plugins.

The tool follows Pi's subagent example extension: one task (``agent`` + ``task``), up to 8
tasks in parallel with at most 4 running at once (``tasks``), or a chain where each step's
``{previous}`` is replaced by the prior step's answer (``chain``). Each task runs a fresh
Agent with its own history, so the main conversation only sees the answer. Pi runs each
subagent as a separate process; here it is an Agent in the same event loop, and
cancelling the call aborts the subagents.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..agent import Agent, RunResult
from ..errors import SubscriptionError, ToolOutcomeUnknownError
from ..hooks import Hooks
from ..limits import RunLimits
from ..messages import AssistantMessage, TextContent
from ..models import ModelInfo
from ..tools import Tool, ToolContext, ToolResult
from ..tasks import _gather_owned
from ._resources import AgentDefinition

TOOL_NAME = "subagent"
MAX_PARALLEL_TASKS = 8
MAX_CONCURRENCY = 4
PER_TASK_OUTPUT_CAP = 50 * 1024  # bytes of UTF-8, per answer in a parallel summary


def _truncate(output: str) -> str:
    """Pi's cap on one answer in a parallel summary; the full answer stays in details."""
    data = output.encode("utf-8")
    if len(data) <= PER_TASK_OUTPUT_CAP:
        return output
    kept = data[:PER_TASK_OUTPUT_CAP].decode("utf-8", errors="ignore")
    omitted = len(data) - len(kept.encode("utf-8"))
    return f"{kept}\n\n[Output truncated: {omitted} bytes omitted. Full output preserved in tool details.]"


def _final_text(result: RunResult) -> str:
    for message in reversed(result.messages):
        if isinstance(message, AssistantMessage):
            return "".join(b.text for b in message.content if isinstance(b, TextContent))
    return ""


def _add_usage(total: dict[str, Any], usage: Mapping[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


def _label(record: dict[str, Any]) -> str:
    if not record["failed"]:
        return "completed"
    return "failed" if record["status"] == "failed" else f"failed ({record['status']})"


def subagent_tool(
    definitions: Sequence[AgentDefinition],
    *,
    make_agent: Callable[[AgentDefinition, Any], Agent],
) -> Tool:
    """Build the tool; `make_agent(definition, main_model)` creates a fresh Agent for one
    task, where `main_model` is the calling agent's current model (None outside a run)."""
    by_name = {d.name: d for d in definitions}
    names = list(by_name)
    listing = "\n".join(f"- {d.name}: {d.description}" for d in definitions)
    agent_name = {"type": "string", "enum": names, "description": "Name of the agent to invoke"}
    item = {
        "type": "object",
        "properties": {
            "agent": agent_name,
            "task": {"type": "string", "description": "Task to delegate to the agent"},
        },
        "required": ["agent", "task"],
        "additionalProperties": False,
    }
    chain_item = {
        "type": "object",
        "properties": {
            "agent": agent_name,
            "task": {
                "type": "string",
                "description": "Task with optional {previous} placeholder for prior output",
            },
        },
        "required": ["agent", "task"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "agent": {**agent_name, "description": "Name of the agent to invoke (for single mode)"},
            "task": {"type": "string", "description": "Task to delegate (for single mode)"},
            "tasks": {
                "type": "array",
                "items": item,
                "description": "Array of {agent, task} for parallel execution",
            },
            "chain": {
                "type": "array",
                "items": chain_item,
                "description": "Array of {agent, task} for sequential execution",
            },
        },
        "additionalProperties": False,
    }
    description = (
        "Delegate tasks to specialized subagents with isolated context.\n"
        "Modes: single (agent + task), parallel (tasks array), chain (sequential with"
        " {previous} placeholder).\n"
        f"Available agents:\n{listing}"
    )

    async def run_one(name: str, task: str, context: ToolContext) -> dict[str, Any]:
        context.cancel.raise_if_cancelled()
        # The main agent's model as of this call, including update_config and hook changes.
        inner = make_agent(by_name[name], getattr(context.agent_context, "model", None))

        result = await context.run_agent(inner, task)
        text = _final_text(result)
        failed = result.status != "completed"
        if failed:
            error = result.errors[-1] if result.errors else result.stop_reason
            text = f"Agent {result.status}: {error}" + (f"\n\n{text}" if text else "")
        record = {
            "agent": name,
            "task": task,
            "status": result.status,
            "failed": failed,
            "output": text,
            "usage": dict(result.usage),
        }
        await context.emit_update({"agent": name, "status": result.status})
        return record

    async def execute(args: dict[str, Any], context: ToolContext) -> ToolResult:
        modes = [
            bool(args.get("agent") and args.get("task")),
            bool(args.get("tasks")),
            bool(args.get("chain")),
        ]
        if sum(modes) != 1:
            return ToolResult.text(
                "Invalid parameters. Provide exactly one mode.\n"
                f"Available agents: {', '.join(names) or 'none'}",
                is_error=True,
            )
        usage: dict[str, Any] = {}
        if args.get("chain"):
            records: list[dict[str, Any]] = []
            previous = ""
            for index, step in enumerate(args["chain"], start=1):
                task = step["task"].replace("{previous}", previous)
                record = await run_one(step["agent"], task, context)
                records.append(record)
                _add_usage(usage, record["usage"])
                if record["failed"]:
                    return ToolResult.text(
                        f"Chain stopped at step {index} ({step['agent']}): {record['output']}",
                        is_error=True,
                        details={"mode": "chain", "results": records},
                        usage=usage,
                    )
                previous = record["output"]
            return ToolResult.text(
                records[-1]["output"] or "(no output)",
                details={"mode": "chain", "results": records},
                usage=usage,
            )
        if args.get("tasks"):
            tasks = args["tasks"]
            if len(tasks) > MAX_PARALLEL_TASKS:
                return ToolResult.text(
                    f"Too many parallel tasks ({len(tasks)}). Max is {MAX_PARALLEL_TASKS}.",
                    is_error=True,
                )
            gate = asyncio.Semaphore(MAX_CONCURRENCY)

            async def limited(entry: dict[str, str]) -> dict[str, Any]:
                async with gate:
                    try:
                        return await run_one(entry["agent"], entry["task"], context)
                    except (SubscriptionError, ToolOutcomeUnknownError):
                        raise
                    except Exception as exc:
                        # One task's fault is that task's failure; the others keep running.
                        error = f"Agent failed: {type(exc).__name__}: {exc}"
                        return {
                            "agent": entry["agent"],
                            "task": entry["task"],
                            "status": "failed",
                            "failed": True,
                            "output": error,
                            "usage": {},
                        }

            records = await _gather_owned(limited(t) for t in tasks)
            for record in records:
                _add_usage(usage, record["usage"])
            succeeded = sum(not r["failed"] for r in records)
            summaries = [
                f"### [{r['agent']}] {_label(r)}\n\n{_truncate(r['output'] or '(no output)')}"
                for r in records
            ]
            return ToolResult.text(
                f"Parallel: {succeeded}/{len(records)} succeeded\n\n"
                + "\n\n---\n\n".join(summaries),
                details={"mode": "parallel", "results": records},
                usage=usage,
            )
        record = await run_one(args["agent"], args["task"], context)
        return ToolResult.text(
            record["output"] or "(no output)",
            is_error=record["failed"],
            details={"mode": "single", "results": [record]},
            usage=record["usage"],
        )

    return Tool(TOOL_NAME, description, schema, execute)


def build_subagent(
    definition: AgentDefinition,
    *,
    tools: Mapping[str, Tool],
    provider: Any,
    stream_fn: Callable[..., Any] | None,
    model: str | ModelInfo,
    hooks: Hooks,
    limits: RunLimits | None,
    skills_prompt: Callable[[list[Tool]], str],
) -> Agent:
    """A fresh Agent for one subagent task."""
    if definition.tools is None:
        chosen = list(tools.values())
    else:
        chosen = [tools[name] for name in definition.tools if name in tools]
    prompt = "\n\n".join(p for p in (definition.system_prompt.strip(), skills_prompt(chosen)) if p)
    source: dict[str, Any] = {}
    if definition.provider is not None:
        source["provider"] = definition.provider
    elif provider is not None:
        source["provider"] = provider
    elif stream_fn is not None:
        source["stream_fn"] = stream_fn
    return Agent(
        **source,
        model=definition.model or model,
        options=dict(definition.options),
        system_prompt=prompt,
        tools=chosen,
        hooks=hooks,
        limits=limits,
    )
