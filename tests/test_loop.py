import asyncio
import pytest
from pi_python import *


def response(*calls, reason="tool_use"):
    return AssistantMessage(list(calls), reason)


def tool(name="t", execute=None, **kwargs):
    async def default(args, context):
        return ToolResult.text("ok")

    return Tool(name, "", {"type": "object"}, execute or default, **kwargs)


async def test_C01_stream_and_end():
    p = ScriptedProvider(
        [
            [
                ModelEvent.boundary("start", 0, TextContent("")),
                ModelEvent.text("he"),
                ModelEvent.text("llo"),
                ModelEvent.boundary("end", 0, TextContent("hello")),
                ModelEvent.done(AssistantMessage.text("hello")),
            ]
        ]
    )
    a = Agent(provider=p)
    events = []
    a.subscribe(events.append)
    r = await a.prompt("go")
    assert r.status == "completed"
    assert r.messages[-1].content[0].text == "hello"
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert events[-1].type == "agent_end"
    assert not a.state.is_running


async def test_C02_result_in_next_request_and_private_details():
    async def execute(args, context):
        return ToolResult.text("public", details={"secret": 1}, structured_content={"value": 2})

    p = ScriptedProvider([response(ToolCall("x", "t", {})), AssistantMessage.text("done")])
    r = await Agent(provider=p, tools=[tool(execute=execute)]).prompt("go")
    assert len(p.requests) == 2 and r.status == "completed"
    m = p.requests[1].messages[-1]
    assert isinstance(m, ToolResultMessage) and m.content[0].text == "public"
    assert m.details is None and not hasattr(m, "structured_content")
    assert next(m for m in r.messages if isinstance(m, ToolResultMessage)).details == {"secret": 1}


@pytest.mark.parametrize(
    "events",
    [
        [],
        # A complete block but no terminal message.
        [ModelEvent.boundary("start", 0, TextContent("")), ModelEvent.text("x")],
        # A delta outside any open block.
        [ModelEvent.text("stray"), ModelEvent.done(response(ToolCall("x", "t", {})))],
        [ModelEvent.done(response(ToolCall("x", "t", {}))), ModelEvent.text("after")],
        [
            ModelEvent.done(response(ToolCall("x", "t", {}))),
            ModelEvent.done(AssistantMessage.text("twice")),
        ],
        # Arguments that never become valid JSON.
        [
            ModelEvent.boundary("start", 0, ToolCall("x", "t", {})),
            ModelEvent.toolcall("{"),
            ModelEvent.boundary("end", 0, ToolCall("x", "t", {})),
            ModelEvent.done(response(ToolCall("x", "t", {}))),
        ],
        # Streamed arguments that disagree with the final call.
        [
            ModelEvent.boundary("start", 0, ToolCall("x", "t", {})),
            ModelEvent.toolcall('{"x":1}'),
            ModelEvent.boundary("end", 0, ToolCall("x", "t", {})),
            ModelEvent.done(response(ToolCall("x", "t", {}))),
        ],
        # Streamed text whose final message is a tool call instead.
        [
            ModelEvent.boundary("start", 0, TextContent("")),
            ModelEvent.text("wrong"),
            ModelEvent.boundary("end", 0, TextContent("wrong")),
            ModelEvent.done(response(ToolCall("x", "t", {}))),
        ],
        [ModelEvent.done(response(ToolCall("x", "t", {}), reason="error"))],
        [ModelEvent.done(response(ToolCall("x", "t", {}), reason="aborted"))],
        [ModelEvent("image")],
    ],
)
async def test_C24_bad_stream_zero_effects(events):
    effects = []

    async def execute(args, ctx):
        effects.append(1)
        return ToolResult.text("bad")

    a = Agent(provider=ScriptedProvider([events]), tools=[tool(execute=execute)])
    r = await a.prompt("go")
    aborted = r.messages[-1].stop_reason == "aborted"  # a provider-reported abort
    assert r.status == ("cancelled" if aborted else "failed") and not effects
    assert all(not m.tool_calls for m in r.messages if isinstance(m, AssistantMessage))
    validate_history(list(a.state.messages))


async def test_C24_error_after_final_is_not_committed():
    class Broken:
        async def stream(self, request, cancel):
            yield ModelEvent.done(response(ToolCall("x", "t", {})))
            raise RuntimeError("tail broke")

    r = await Agent(provider=Broken(), tools=[tool()]).prompt("go")
    assert r.status == "failed" and not r.tool_outcomes
    assert "tail broke" in r.stop_reason


async def test_C10_truncation_all_tools_fail_without_execution():
    effects = []

    async def execute(args, ctx):
        effects.append(1)
        return ToolResult.text("bad")

    p = ScriptedProvider(
        [
            response(ToolCall("1", "t", {}), ToolCall("2", "t", {}), reason="length"),
            AssistantMessage.text("retry later"),
        ]
    )
    r = await Agent(provider=p, tools=[tool(execute=execute)]).prompt("go")
    assert r.status == "completed" and not effects
    assert len(r.tool_outcomes) == 2
    assert all(o.result.error_code == "truncated" for o in r.tool_outcomes)


async def test_C11_transform_before_convert_defensive_copies():
    order = []

    def transform(messages, cancel):
        order.append("transform")
        messages[0].content = "changed"
        return messages + [CustomMessage("x", {"text": "custom"})]

    def convert(messages):
        order.append("convert")
        return [
            UserMessage(m.data["text"]) if isinstance(m, CustomMessage) else m for m in messages
        ]

    p = ScriptedProvider([AssistantMessage.text("done")])
    a = Agent(provider=p, hooks=Hooks(transform_context=transform, convert_to_llm=convert))
    await a.prompt("original")
    assert order == ["transform", "convert"]
    assert a.state.messages[0].content == "original"
    assert p.requests[0].messages[0].content == "changed"
    snapshot = a.state
    snapshot.messages[0].content = "tampered"
    assert a.state.messages[0].content == "original"


async def test_C15_C23_run_updates_persist_but_not_to_defaults():
    seen = []

    def prepare(ctx, cancel):
        seen.append(ctx.model)
        return TurnUpdate(model="run-model", options={"x": 1}) if len(seen) == 1 else None

    class Mutating(ScriptedProvider):
        async def stream(self, request, cancel):
            async for e in super().stream(request, cancel):
                request.options["x"] = 99
                request.messages.clear()
                yield e

    p = Mutating(
        [
            response(ToolCall("x", "t", {})),
            AssistantMessage.text("done"),
            AssistantMessage.text("next"),
        ]
    )
    a = Agent(provider=p, model="default", tools=[tool()], hooks=Hooks(prepare_request=prepare))
    await a.prompt("go")
    await a.prompt("new")
    assert seen == ["default", "run-model", "default"]
    assert [r.options for r in p.requests] == [{"x": 1}, {"x": 1}, {}]
    assert a.state.messages[0].role == "system"


async def test_C13_C14_queues_and_modes():
    p = ScriptedProvider(
        [response(ToolCall("x", "t", {})), *[AssistantMessage.text(str(i)) for i in range(4)]]
    )
    a = Agent(provider=p, tools=[tool()])

    def listener(event):
        if event.type == "tool_execution_start":
            a.steer("guide1")
            a.steer("guide2")
            a.follow_up("later1")
            a.follow_up("later2")

    a.subscribe(listener)
    r = await a.prompt("go")
    assert r.status == "completed" and len(p.requests) == 5
    assert [q.messages[-1].content for q in p.requests[1:]] == [
        "guide1",
        "guide2",
        "later1",
        "later2",
    ]
    p = ScriptedProvider([AssistantMessage.text("first"), AssistantMessage.text("second")])
    a = Agent(provider=p, steering_mode="all", follow_up_mode="all")
    a.steer("s1")
    a.steer("s2")
    a.follow_up("f1")
    a.follow_up("f2")
    await a.prompt("go")
    assert [m.content for m in p.requests[0].messages] == ["go", "s1", "s2"]
    assert [m.content for m in p.requests[1].messages[-2:]] == ["f1", "f2"]


async def test_C15_steering_during_next_turn_and_updates():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def prepare(ctx, cancel):
        entered.set()
        await release.wait()
        return TurnUpdate(model="next", messages=[UserMessage("prepared")])

    p = ScriptedProvider([response(ToolCall("x", "t", {})), AssistantMessage.text("done")])
    a = Agent(provider=p, tools=[tool()], hooks=Hooks(prepare_next_turn=prepare))
    task = asyncio.create_task(a.prompt("go"))
    await entered.wait()
    a.steer("during")
    a.update_config(AgentConfigUpdate(options={"v": 2}))
    release.set()
    await task
    assert [m.content for m in p.requests[1].messages[-2:]] == ["prepared", "during"]
    assert p.requests[1].model == "next" and p.requests[1].options == {"v": 2}


@pytest.mark.parametrize(
    "terminates,decision,expected",
    [
        ([True, True], None, 1),
        ([True, False], None, 2),
        ([False, False], "continue", 2),
        ([True, True], "continue", 2),
        ([False, False], "end", 1),
    ],
)
async def test_C16_terminate_and_finish(terminates, decision, expected):
    async def execute(args, ctx):
        return ToolResult.text("ok", terminate=terminates[int(ctx.call_id)])

    count = 0

    def finish(ctx, cancel):
        nonlocal count
        count += 1
        return decision if count == 1 else None

    p = ScriptedProvider(
        [response(ToolCall("0", "t", {}), ToolCall("1", "t", {})), AssistantMessage.text("done")]
    )
    a = Agent(provider=p, tools=[tool(execute=execute)], hooks=Hooks(finish_turn=finish))
    await a.prompt("go")
    assert len(p.requests) == expected


async def test_C16_end_preserves_queue_error_calls_finish():
    p = ScriptedProvider([AssistantMessage.text("error", stop_reason="error")])
    seen = []
    a = Agent(
        provider=p,
        hooks=Hooks(
            finish_turn=lambda ctx, cancel: seen.append(ctx.message.stop_reason) or "continue"
        ),
    )
    a.follow_up("later")
    r = await a.prompt("go")
    assert seen == ["error"] and r.status == "failed" and r.queued_follow_up == 1


async def test_C12_runtime_tool_declarations_track_config():
    p = ScriptedProvider([response(ToolCall("x", "t", {})), AssistantMessage.text("done")])
    a = Agent(provider=p, tools=[tool()])

    def listener(e):
        if e.type == "turn_end":
            a.update_config(AgentConfigUpdate(tools=[tool("u")]))

    a.subscribe(listener)
    await a.prompt("go")
    assert [t.name for t in p.requests[1].tools] == ["u"]
    delta = p.requests[1].messages[-1]
    assert delta.tools_removed == ["t"] and delta.tools_added[0].name == "u"


async def test_unchecked_provider_failure_keeps_partial_and_starts_once():
    class Failing:
        name = "custom-provider"

        async def stream(self, request, cancel):
            yield ModelEvent.boundary("start", 0, TextContent(""))
            yield ModelEvent.text("partial answer", 0)
            raise RuntimeError("prompt is too long")

    events = []
    a = Agent(provider=Failing(), model="m1")
    a.subscribe(lambda e: events.append(e.type))
    r = await a.prompt("go")
    message = r.messages[-1]
    assert r.status == "failed" and message.content[0].text == "partial answer"
    assert (message.provider, message.model) == ("custom-provider", "m1")
    assert "prompt is too long" in message.error and "prompt is too long" in r.stop_reason
    assert events.count("message_start") == 2  # the user prompt, then the response once
    assert events[-2:] == ["turn_end", "agent_end"]
