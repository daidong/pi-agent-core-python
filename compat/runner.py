"""Python side of the shared JSON fixtures; comparison keeps calls, inputs and ordering."""

from __future__ import annotations
import asyncio
from copy import deepcopy
import json
from pi_python import (
    Agent,
    Hooks,
    ModelEvent,
    TextContent,
    Tool,
    ToolResult,
    ToolResultUpdate,
    TurnUpdate,
    UserMessage,
    message_from_dict,
    message_to_dict,
)


def normalize(message):
    d = message_to_dict(message)
    for key in ("timestamp", "usage", "provider", "model", "error", "thinking_level"):
        d.pop(key, None)
    for key in ("sections", "tools_added", "tools_removed"):
        if not d.get(key):
            d.pop(key, None)
    return d


class FixtureProvider:
    """Scripted responses; a `stream` response streams its text, then fails or awaits abort."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def stream(self, request, cancel):
        self.requests.append(deepcopy(request))
        spec = next(self.responses, None)
        if spec is None:
            raise RuntimeError("Fixture responses exhausted")
        message = message_from_dict({k: v for k, v in spec.items() if k != "stream"})
        if not spec.get("stream"):
            yield ModelEvent.done(message)
            return
        for index, block in enumerate(message.content):
            yield ModelEvent.boundary("start", index, TextContent(""))
            yield ModelEvent.text(block.text, index)
        if message.stop_reason == "aborted":
            await cancel.wait()
        yield ModelEvent("error", message=message, reason=message.stop_reason)


async def run_fixture(fixture):
    effects, events, hooks = [], [], []
    barriers = {}

    def barrier(id):
        return barriers.setdefault(id, asyncio.Event())

    tools = []
    for spec in fixture.get("tools", []):

        async def execute(args, context, spec=spec):
            effects.append({"name": spec["name"], "arguments": deepcopy(args)})
            if spec.get("wait_for"):
                await barrier(spec["wait_for"]).wait()
            if spec.get("wait_abort"):
                await asyncio.Event().wait()  # cancellation is Python's abort signal
            if spec.get("error"):
                raise RuntimeError(spec["error"])
            return ToolResult.text(
                spec.get("result", json.dumps(args, separators=(",", ":"))),
                structured_content=spec.get("structured_content"),
                terminate=spec.get("terminate", False),
            )

        def prepare(args, spec=spec):
            key = spec["prepare_number"]
            args[key] = int(args[key])
            return args

        tools.append(
            Tool(
                spec["name"],
                spec.get("description", ""),
                spec.get("input_schema", {"type": "object"}),
                execute,
                execution_mode=spec.get("execution_mode", "parallel"),
                prepare_arguments=prepare if spec.get("prepare_number") else None,
            )
        )
    provider = FixtureProvider(fixture["responses"])
    finish = iter(fixture.get("finish", []))
    count = 0

    def prepare_request(ctx, cancel):
        nonlocal count
        hooks.append("prepare_request")
        count += 1
        return TurnUpdate(model=fixture["prepare_model"]) if count == 1 else None

    def next_turn(ctx, cancel):
        hooks.append("prepare_next_turn")
        return TurnUpdate(messages=[UserMessage(fixture["next_message"])])

    def transform(messages, cancel):
        hooks.append("transform")
        return messages + [UserMessage("transformed")]

    def convert(messages):
        hooks.append("convert")
        return messages

    def finish_turn(ctx, cancel):
        hooks.append("finish_turn")
        return next(finish, None)

    def before(call, args, ctx):
        error = fixture.get("before_error", {}).get(call.id)
        if error:
            raise RuntimeError(error)
        return call.id not in fixture.get("block_ids", [])

    agent = Agent(
        provider=provider,
        system_prompt=fixture.get("system_prompt", ""),
        tools=tools,
        messages=[message_from_dict(m) for m in fixture.get("history", [])],
        execution_mode=fixture.get("execution_mode", "parallel"),
        steering_mode=fixture.get("steering_mode", "one-at-a-time").replace("-", "_"),
        follow_up_mode=fixture.get("follow_up_mode", "one-at-a-time").replace("-", "_"),
        hooks=Hooks(
            before_tool_call=before
            if fixture.get("block_ids") or fixture.get("before_error")
            else None,
            after_tool_call=(
                lambda call, result, ctx: ToolResultUpdate(
                    content=ToolResult.text(fixture["after_text"]).content
                )
            )
            if fixture.get("after_text")
            else None,
            finish_turn=finish_turn if "finish" in fixture else None,
            transform_context=transform if fixture.get("transform") else None,
            convert_to_llm=convert if fixture.get("transform") else None,
            prepare_request=prepare_request if fixture.get("prepare_model") else None,
            prepare_next_turn=next_turn if fixture.get("next_message") else None,
        ),
    )
    operations = deepcopy(fixture.get("operations", []))

    def listener(event):
        item = {"type": event.type}
        if event.call_id:
            item["call_id"] = event.call_id
        if "message" in event.data:
            item["message"] = normalize(message_from_dict(event.data["message"]))
        events.append(item)
        if event.type == "tool_execution_end":
            barrier(event.call_id).set()
        for op in operations:
            if op["event"] != event.type or op.get("done"):
                continue
            if op.get("delta_type") and event.data.get("delta_type") != op["delta_type"]:
                continue
            op["done"] = True
            if op["method"] == "abort":
                agent.abort()
            else:
                getattr(agent, op["method"])(message_from_dict(op["message"]))

    agent.subscribe(listener)
    prompt = (
        fixture["prompt"]
        if isinstance(fixture["prompt"], str)
        else message_from_dict(fixture["prompt"])
    )
    result = await agent.prompt(prompt)
    return {
        "requests": [[normalize(m) for m in r.messages] for r in provider.requests],
        "request_models": [r.model for r in provider.requests],
        "effects": effects,
        "events": events,
        "messages": [normalize(m) for m in agent.state.messages],
        "hooks": hooks,
        "status": result.status,
    }


def map_errors(trace):
    """Only map specifically recognized error categories; retain tool name and call ID."""
    trace = deepcopy(trace)

    def visit(value):
        if isinstance(value, dict):
            # A streamed response has no stop reason yet when it starts; Python marks it pending.
            started = value.get("message") if value.get("type") == "message_start" else None
            if isinstance(started, dict) and started.get("role") == "assistant":
                started.pop("stop_reason", None)
            if value.get("role") == "user" and isinstance(value.get("content"), list):
                assert all(b.get("type") == "text" for b in value["content"])
                value["content"] = "".join(b["text"] for b in value["content"])
            if value.get("role") == "tool_result" and value.get("is_error"):
                text = "\n".join(b["text"] for b in value["content"])
                if "output token limit" in text or "output length limit" in text:
                    category = "truncated"
                elif "blocked" in text.lower():
                    category = "blocked"
                elif text in {"boom", "RuntimeError: boom"}:
                    category = "tool_error:boom"
                elif text in {"denied", "RuntimeError: denied"}:
                    category = "hook_error:denied"
                elif "Unknown tool:" in text or "not found" in text:
                    category = "unknown_tool:" + value["name"]
                else:
                    category = None
                if category is not None:
                    value["content"] = [{"type": "text", "text": category}]
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(trace)
    return trace
