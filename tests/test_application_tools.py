"""D9 / C28: application batches share execution, not model turns."""

import asyncio
import json
from dataclasses import asdict

import pytest
from pi_python import *


def make_agent(
    execute,
    *,
    hooks=None,
    limits=None,
    execution_mode="parallel",
    include_tool_origin=False,
    **tool_options,
):
    provider = ScriptedProvider([AssistantMessage.text("done")])
    agent = Agent(
        provider=provider,
        tools=[Tool("check", "", {"type": "object"}, execute, **tool_options)],
        hooks=hooks,
        limits=limits,
        execution_mode=execution_mode,
        include_tool_origin=include_tool_origin,
    )
    return agent, provider


def batch_record(messages):
    records = [m for m in messages if isinstance(m, CustomMessage)]
    assert len(records) == 1 and records[0].custom_type == "application_tool_batch"
    assert not any(isinstance(m, (AssistantMessage, ToolResultMessage)) for m in messages)
    validate_history(messages)
    assert decode_messages(encode_messages(messages)) == messages
    return records[0].data["calls"]


async def test_C28_no_model_or_turn_hooks_and_private_feedback():
    def forbidden(*args):
        pytest.fail("application execution entered a model-only hook")

    async def execute(args, ctx):
        assert ctx.origin == "application" and ctx.source == "checks@1"
        assert ctx.assistant_message is None
        await ctx.emit_update({"progress": 1})
        return ToolResult.text(
            "visible",
            details={"secret": "local"},
            structured_content={"private": 1},
            usage={"private_cost": 1},
            terminate=True,
        )

    hooks = Hooks(
        **{
            name: forbidden
            for name in (
                "prepare_request",
                "prepare_next_turn",
                "transform_context",
                "convert_to_llm",
                "finish_turn",
            )
        }
    )
    agent, provider = make_agent(execute, hooks=hooks, include_tool_origin=True)
    events = []
    agent.subscribe(events.append)
    agent.steer("guidance")
    agent.follow_up("later")
    result = await agent.execute_tools([ToolInvocation("check", {})], source="checks@1")
    assert result.status == "completed" and result.usage == {}
    assert provider.requests == []
    assert result.queued_steering == result.queued_follow_up == 1
    record = batch_record(result.messages)[0]
    outcome = result.tool_outcomes[0]
    assert record["id"] == outcome.call.id
    assert (outcome.origin, outcome.source) == ("application", "checks@1")
    assert outcome.raw_result.details == {"secret": "local"}
    assert "secret" not in encode_messages(result.messages)
    assert "private" not in encode_messages(result.messages)
    assert events[0].type == "agent_start" and events[-1].type == "agent_end"
    assert all(e.turn_id is None for e in events)
    assert not any(e.type.startswith("turn_") for e in events)
    for event in events:
        assert decode_event(encode_event(event)) == event
        if event.type.startswith("tool_execution_"):
            assert event.data["origin"] == "application" and event.data["source"] == "checks@1"
    agent.hooks = Hooks()
    agent.clear_all_queues()
    await agent.prompt("interpret")
    request_text = json.dumps([asdict(m) for m in provider.requests[0].messages])
    assert "Application tool execution results" in request_text and "visible" in request_text
    assert "secret" not in request_text and "private" not in request_text
    assert all(not isinstance(m, CustomMessage) for m in provider.requests[0].messages)


@pytest.mark.parametrize("origin", ["application", "model"])
@pytest.mark.parametrize("mode", ["parallel", "sequential"])
async def test_C28_shared_preparation_hooks_errors_and_output_checks(origin, mode):
    trace = []

    def prepare(args):
        trace.append(("prepare", args["n"]))
        return {"n": int(args["n"])}

    def before(call, args, ctx):
        assert ctx.origin == origin
        assert ctx.source == ("pack" if origin == "application" else None)
        n = args["n"]
        args["n"] = 99  # cannot mutate execution arguments
        return n != 2

    async def execute(args, ctx):
        n = args["n"]
        trace.append(("execute", n))
        if n == 3:
            raise ValueError("known failure")
        return ToolResult.text(str(n), structured_content={"n": n})

    def after(call, result, ctx):
        trace.append(("after", ctx.args["n"]))
        if ctx.args["n"] == 4:
            raise ValueError("postprocessing failed")

    tool = Tool(
        "check",
        "",
        {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
        execute,
        prepare_arguments=prepare,
        output_schema={"type": "object", "properties": {"n": {"maximum": 4}}},
    )
    calls = [
        ToolCall(str(i), "check", {"n": n}) for i, n in enumerate(["1", "2", "3", "4", "5", "bad"])
    ]
    calls.append(ToolCall("missing", "absent", {}))
    provider = ScriptedProvider(
        [AssistantMessage(calls, "tool_use"), AssistantMessage.text("done")]
    )
    agent = Agent(
        provider=provider,
        tools=[tool],
        execution_mode=mode,
        include_tool_origin=True,
        hooks=Hooks(before_tool_call=before, after_tool_call=after),
    )
    events = []
    agent.subscribe(events.append)
    if origin == "application":
        result = await agent.execute_tools(
            [ToolInvocation(c.name, c.arguments) for c in calls], source="pack"
        )
        batch_record(result.messages)
        assert not provider.requests
    else:
        result = await agent.prompt("go")
        assert len(provider.requests) == 2
    assert result.status == "completed"
    assert [o.execution_status for o in result.tool_outcomes] == [
        "succeeded",
        "not_started",
        "failed",
        "succeeded",
        "succeeded",
        "not_started",
        "not_started",
    ]
    assert [o.result.is_error for o in result.tool_outcomes] == [
        False,
        True,
        True,
        True,
        True,
        True,
        True,
    ]
    assert result.tool_outcomes[3].raw_result.content == [TextContent("4")]
    assert result.tool_outcomes[0].prepared_arguments == {"n": 1}
    assert ("execute", 99) not in trace and ("execute", 2) not in trace
    assert all(o.origin == origin for o in result.tool_outcomes)
    assert all(e.data["origin"] == origin for e in events if e.type.startswith("tool_execution_"))


@pytest.mark.parametrize("origin", ["application", "model"])
async def test_C28_shared_budget_rejects_entire_batch(origin):
    async def execute(args, ctx):
        pytest.fail("over-budget batch executed")

    agent, provider = make_agent(execute, limits=RunLimits(max_tool_calls=1))
    if origin == "application":
        result = await agent.execute_tools([ToolInvocation("check", {})] * 2)
        assert not provider.requests
        assert len(batch_record(result.messages)) == 2
    else:
        provider.responses = iter(
            [AssistantMessage([ToolCall(str(i), "check", {}) for i in range(2)], "tool_use")]
        )
        result = await agent.prompt("go")
    assert result.status == "limit_reached" and result.stop_reason == "max_tool_calls"
    assert all(
        o.execution_status == "not_started" and o.result.error_code == "limit"
        for o in result.tool_outcomes
    )


@pytest.mark.parametrize("caller_cancel", [False, True])
async def test_C28_cancellation_preserves_completed_and_unstarted(caller_cancel):
    entered = asyncio.Event()

    async def execute(args, ctx):
        if args["n"] == 1:
            entered.set()
            await asyncio.Event().wait()
        return ToolResult.text("ok")

    agent, provider = make_agent(execute, execution_mode="sequential")
    task = asyncio.create_task(
        agent.execute_tools([ToolInvocation("check", {"n": n}) for n in range(3)])
    )
    await asyncio.wait_for(entered.wait(), 2)
    if caller_cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        records = batch_record(list(agent.state.messages))
    else:
        agent.abort()
        result = await task
        assert result.status == "cancelled"
        records = batch_record(result.messages)
    assert [r["execution_status"] for r in records] == ["succeeded", "cancelled", "not_started"]
    assert not agent.state.is_running and not provider.requests
    await agent.wait_for_idle()


@pytest.mark.parametrize("kind", ["tool", "run"])
async def test_C28_timeouts(kind):
    async def execute(args, ctx):
        await asyncio.Event().wait()

    limits = RunLimits(**{f"{kind}_timeout": 0.02})
    agent, _ = make_agent(execute, limits=limits)
    result = await agent.execute_tools([ToolInvocation("check", {})])
    assert result.status == ("completed" if kind == "tool" else "limit_reached")
    assert result.tool_outcomes[0].execution_status == "cancelled"
    assert batch_record(result.messages)[0]["is_error"]


async def test_C28_unknown_result_stops_and_locks_instance():
    effects = []

    async def execute(args, ctx):
        effects.append(1)
        raise ToolOutcomeUnknownError("external outcome unknown")

    agent, _ = make_agent(execute, execution_mode="sequential")
    result = await agent.execute_tools([ToolInvocation("check", {})] * 2)
    assert result.status == "failed" and result.reconciliation_required
    assert effects == [1]
    assert [r["execution_status"] for r in batch_record(result.messages)] == [
        "unknown",
        "not_started",
    ]
    with pytest.raises(ToolOutcomeUnknownError):
        await agent.execute_tools([ToolInvocation("check", {})])


@pytest.mark.parametrize(
    "event_type",
    ["agent_start", "tool_execution_start", "tool_execution_end", "message_end", "agent_end"],
)
async def test_C28_subscriber_failure_records_accepted_calls(event_type):
    effects = []

    async def execute(args, ctx):
        effects.append(1)
        return ToolResult.text("ok")

    def listener(event):
        if event.type == event_type:
            raise RuntimeError("recorder failed")

    agent, _ = make_agent(execute, execution_mode="sequential")
    agent.subscribe(listener)
    result = await agent.execute_tools([ToolInvocation("check", {})] * 2)
    assert result.status == "failed" and agent.state.diagnostics
    records = batch_record(result.messages)
    assert len(records) == 2
    if event_type in {"agent_start", "tool_execution_start"}:
        assert not effects
    elif event_type == "tool_execution_end":
        assert effects == [1] and records[0]["execution_status"] == "succeeded"


async def test_C28_busy_configuration_snapshot_and_end_delivery():
    entered, release, end_entered, end_release = [asyncio.Event() for _ in range(4)]
    effects = []

    async def old(args, ctx):
        effects.append("old")
        entered.set()
        await release.wait()
        return ToolResult.text("old")

    async def new(args, ctx):
        effects.append("new")
        return ToolResult.text("new")

    agent, provider = make_agent(old, execution_mode="sequential")

    async def listener(event):
        if event.type == "agent_end":
            end_entered.set()
            await end_release.wait()

    agent.subscribe(listener)
    task = asyncio.create_task(agent.execute_tools([ToolInvocation("check", {})] * 2))
    await asyncio.wait_for(entered.wait(), 2)
    for operation in (
        agent.prompt("no"),
        agent.continue_run(),
        agent.execute_tools([ToolInvocation("check", {})]),
    ):
        with pytest.raises(AgentBusyError):
            await operation
    agent.update_config(AgentConfigUpdate(tools=[Tool("check", "", {}, new)], options={"v": 1}))
    release.set()
    await asyncio.wait_for(end_entered.wait(), 2)
    waiter = asyncio.create_task(agent.wait_for_idle())
    await asyncio.sleep(0)
    assert not waiter.done() and not task.done()
    end_release.set()
    await task
    await waiter
    assert effects == ["old", "old"]
    agent.update_config(AgentConfigUpdate(options={"v": 2}))
    await agent.execute_tools([ToolInvocation("check", {})])
    assert effects[-1] == "new"
    await agent.prompt("next")
    assert provider.requests[0].options == {"v": 2}


async def test_C28_no_provider_empty_invalid_closed_and_detached_inputs():
    async def execute(args, ctx):
        return ToolResult.text(str(args["n"]))

    agent = Agent(tools=[Tool("check", "", {}, execute)])
    for values in ([], [ToolCall("caller-id", "check", {})]):
        with pytest.raises(ConfigurationError):
            await agent.execute_tools(values)
    assert not agent.state.is_running
    with pytest.raises(ConfigurationError):
        await agent.execute_tools([ToolInvocation("check", {})], source=1)
    invocation = ToolInvocation("check", {"n": 1})
    agent.subscribe(lambda event: invocation.arguments.update(n=2))
    result = await agent.execute_tools([invocation])
    assert result.tool_outcomes[0].original_arguments == {"n": 1}
    assert result.tool_outcomes[0].result.content == [TextContent("1")]
    await agent.aclose()
    with pytest.raises(AgentClosedError):
        await agent.execute_tools([invocation])


async def test_C28_imported_feedback_allowlist_and_other_custom_types():
    async def execute(args, ctx):
        return ToolResult.text("ok")

    agent, _ = make_agent(execute)
    result = await agent.execute_tools([ToolInvocation("check", {})])
    custom = result.messages[0]
    custom.data["secret"] = "top-level-secret"
    custom.data["calls"][0]["raw_result"] = "raw-secret"
    provider = ScriptedProvider([AssistantMessage.text("done")])
    restored = Agent(provider=provider, messages=decode_messages(encode_messages(result.messages)))
    assert not provider.requests
    await restored.prompt("interpret")
    assert "secret" not in json.dumps([asdict(m) for m in provider.requests[0].messages])
    other = Agent(provider=provider, messages=[CustomMessage("other", {})])
    failed = await other.prompt("no converter")
    assert failed.status == "failed" and "Custom messages require" in str(failed.errors)


async def test_C28_concurrency_is_bounded_and_results_in_call_order():
    active = peak = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(args, ctx):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1
        return ToolResult.text(str(args["n"]))

    agent, _ = make_agent(execute, limits=RunLimits(max_concurrency=2))
    task = asyncio.create_task(
        agent.execute_tools([ToolInvocation("check", {"n": n}) for n in range(4)])
    )
    await asyncio.wait_for(entered.wait(), 2)
    release.set()
    result = await task
    assert peak == 2
    assert [r["arguments"]["n"] for r in batch_record(result.messages)] == list(range(4))


async def test_C28_cleanup_deadline_retains_unstopped_call_without_replay():
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def execute(args, ctx):
        effects.append(1)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return ToolResult.text("late success")

    agent, _ = make_agent(execute, limits=RunLimits(cleanup_timeout=0.02))
    task = asyncio.create_task(agent.execute_tools([ToolInvocation("check", {})]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        agent.abort()
        result = await asyncio.wait_for(task, 2)
        assert result.status == "cancelled" and not result.cleanup_complete
        record = batch_record(result.messages)[0]
        assert record["execution_status"] == "running" and record["error_code"] == "not_stopped"
        with pytest.raises(CleanupTimeoutError):
            await agent.execute_tools([ToolInvocation("check", {})])
    finally:
        release.set()

        async def wait_for_cleanup():
            while not agent.state.cleanup_complete:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_cleanup(), 2)
        await agent.aclose()
    assert effects == [1]
    # Late completion cannot rewrite the committed record or imply a replay.
    assert batch_record(list(agent.state.messages))[0]["execution_status"] == "running"


@pytest.mark.parametrize("entry", ["hook", "subscriber"])
async def test_C28_reentrant_application_execution_is_rejected(entry):
    attempts = []

    async def execute(args, ctx):
        return ToolResult.text("ok")

    agent, _ = make_agent(execute)

    async def reenter(*args):
        with pytest.raises(AgentBusyError):
            await agent.execute_tools([ToolInvocation("check", {})])
        attempts.append(1)

    if entry == "hook":
        agent.hooks.before_tool_call = reenter
    else:

        async def listener(event):
            if event.type == "agent_start":
                await reenter()

        agent.subscribe(listener)
    result = await agent.execute_tools([ToolInvocation("check", {})])
    assert result.status == "completed" and attempts == [1]


async def test_C28_model_run_excludes_application_batch():
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(args, ctx):
        entered.set()
        await release.wait()
        return ToolResult.text("ok")

    agent, provider = make_agent(execute)
    provider.responses = iter(
        [
            AssistantMessage([ToolCall("model", "check", {})], "tool_use"),
            AssistantMessage.text("done"),
        ]
    )
    task = asyncio.create_task(agent.prompt("go"))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(AgentBusyError):
            await agent.execute_tools([ToolInvocation("check", {})])
    finally:
        release.set()
        result = await task
    assert result.status == "completed" and len(result.tool_outcomes) == 1
    assert result.tool_outcomes[0].origin == "model"


@pytest.mark.parametrize("turn_id", [-1, True, 1.2, "1"])
def test_C28_nullable_turn_id_still_rejects_invalid_values(turn_id):
    with pytest.raises(MessageValidationError):
        encode_event(Event("agent_start", "run", turn_id, 1))
