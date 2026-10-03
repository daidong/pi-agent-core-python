import asyncio
import pytest
from pi_python import *


def batch(*names):
    return AssistantMessage([ToolCall(name, name, {}) for name in names], "tool_use")


def make_tool(name, fn):
    return Tool(name, "", {"type": "object"}, fn)


async def test_C17_busy_and_continue():
    entered = asyncio.Event()
    release = asyncio.Event()

    class Waiting:
        async def stream(self, request, cancel):
            entered.set()
            await release.wait()
            yield ModelEvent.done(AssistantMessage.text("done"))

    a = Agent(provider=Waiting())
    with pytest.raises(InvalidContinuationError):
        await a.continue_run()
    task = asyncio.create_task(a.prompt("go"))
    await entered.wait()
    with pytest.raises(AgentBusyError):
        await a.prompt("no")
    with pytest.raises(AgentBusyError):
        await a.continue_run()
    release.set()
    await task
    with pytest.raises(InvalidContinuationError):
        await a.continue_run()
    await a.aclose()
    with pytest.raises(AgentClosedError):
        await a.prompt("no")


async def test_C18_end_subscription_delays_idle():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def listener(e):
        if e.type == "agent_end":
            entered.set()
            await release.wait()

    a = Agent(provider=ScriptedProvider([AssistantMessage.text("done")]))
    a.subscribe(listener)
    task = asyncio.create_task(a.prompt("go"))
    await entered.wait()
    assert a.state.is_running and not task.done()
    idle = asyncio.create_task(a.wait_for_idle())
    await asyncio.sleep(0)
    assert not idle.done()
    release.set()
    await task
    await idle
    assert not a.state.is_running


async def test_C18_subscription_failure_stops_without_recursion():
    seen = []

    def broken(e):
        seen.append(e.type)
        if e.type == "message_end":
            raise ValueError("recorder failed")

    other = []
    a = Agent(provider=ScriptedProvider([AssistantMessage.text("unused")]))
    a.subscribe(broken)
    a.subscribe(other.append)
    r = await a.prompt("go")
    assert r.status == "failed"
    assert seen.count("message_end") == 1
    assert a.state.diagnostics[-1]["unhandled_listener_indices"] == [1]
    assert "agent_end" not in seen


@pytest.mark.parametrize("caller_cancel", [False, True])
async def test_C19_cancel_propagates_and_stream_closes(caller_cancel):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Waiting:
        async def stream(self, request, cancel):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield ModelEvent.done(AssistantMessage.text("never"))
            finally:
                closed.set()

    a = Agent(provider=Waiting())
    task = asyncio.create_task(a.prompt("go"))
    await entered.wait()
    a.follow_up("keep")
    if caller_cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        a.abort()
        r = await task
        assert r.status == "cancelled" and r.queued_follow_up == 1 and r.cleanup_complete
    await a.wait_for_idle()
    assert closed.is_set()


async def test_C20_abort_running_tool_is_an_error_result_and_agent_stays_usable():
    waiting = asyncio.Event()
    fast_done = asyncio.Event()
    effects = []
    seen = []

    async def fast(args, ctx):
        effects.append("fast")
        return ToolResult.text("saved")

    async def slow(args, ctx):
        effects.append("slow")
        waiting.set()
        await asyncio.Event().wait()

    async def finish(ctx, cancel):
        seen.append(ctx.message.stop_reason)

    p = ScriptedProvider([batch("fast", "slow"), AssistantMessage.text("again")])
    a = Agent(
        provider=p,
        tools=[make_tool("fast", fast), make_tool("slow", slow)],
        hooks=Hooks(finish_turn=finish),
    )

    def listen(e):
        if e.type == "tool_execution_end" and e.call_id == "fast":
            fast_done.set()

    a.subscribe(listen)
    task = asyncio.create_task(a.prompt("go"))
    await waiting.wait()
    await fast_done.wait()
    a.abort()
    r = await task
    # Pi: the batch settles, then one more turn ends with an aborted response.
    assert r.status == "cancelled" and not r.reconciliation_required
    assert r.tool_outcomes[0].result.content[0].text == "saved"
    assert r.tool_outcomes[1].execution_status == "cancelled"
    assert r.tool_outcomes[1].result.content[0].text == "Operation aborted"
    assert [m.role for m in r.messages] == [
        "user",
        "assistant",
        "tool_result",
        "tool_result",
        "assistant",
    ]
    assert r.messages[-1].stop_reason == "aborted" and seen == ["tool_use", "aborted"]
    assert len(p.requests) == 1 and effects == ["fast", "slow"]
    validate_history(list(a.state.messages))
    again = await a.prompt("again")
    assert again.status == "completed" and len(p.requests) == 2


async def test_C20_abort_mid_stream_finishes_the_turn_with_partial_content():
    finished, events = [], []

    class Streaming:
        name = "anthropic"

        async def stream(self, request, cancel):
            yield ModelEvent.boundary("start", 0, TextContent(""))
            yield ModelEvent.text("partial", 0)
            await asyncio.Event().wait()

    async def finish(ctx, cancel):
        finished.append((ctx.message.stop_reason, cancel.cancelled))

    a = Agent(provider=Streaming(), model="claude-x", hooks=Hooks(finish_turn=finish))
    a.subscribe(lambda e: events.append(e.type))
    a.subscribe(lambda e: a.abort() if e.data.get("delta") == "partial" else None)
    r = await a.prompt("go")
    message = r.messages[-1]
    assert r.status == "cancelled" and finished == [("aborted", True)]
    assert message.stop_reason == "aborted" and message.content[0].text == "partial"
    assert (message.provider, message.model) == ("anthropic", "claude-x")
    assert events.count("message_start") == 2  # user, then the streamed response
    assert events[-3:] == ["message_end", "turn_end", "agent_end"]
    assert a.state.last_error and "aborted" in a.state.last_error


async def test_C20_explicit_unknown_stops_current_run():
    async def unknown(args, ctx):
        raise ToolOutcomeUnknownError("submission uncertain")

    p = ScriptedProvider([batch("t"), AssistantMessage.text("must not run")])
    r = await Agent(provider=p, tools=[make_tool("t", unknown)]).prompt("go")
    assert r.status == "failed" and r.reconciliation_required and len(p.requests) == 1


async def test_C21_batch_budget_is_atomic():
    effects = []

    async def execute(args, ctx):
        effects.append(1)
        return ToolResult.text("bad")

    r = await Agent(
        provider=ScriptedProvider([batch("a", "b")]),
        tools=[make_tool("a", execute), make_tool("b", execute)],
        limits=RunLimits(max_tool_calls=1),
    ).prompt("go")
    assert r.status == "limit_reached" and not effects
    assert len(r.tool_outcomes) == 2 and all(
        o.result.error_code == "limit" for o in r.tool_outcomes
    )


async def test_C21_model_budget_and_total_timeout():
    async def execute(args, ctx):
        return ToolResult.text("ok")

    p = ScriptedProvider([batch("t")])
    r = await Agent(
        provider=p, tools=[make_tool("t", execute)], limits=RunLimits(max_model_requests=1)
    ).prompt("go")
    assert r.status == "limit_reached" and r.stop_reason == "max_model_requests"

    class Waiting:
        async def stream(self, request, cancel):
            await asyncio.Event().wait()
            yield ModelEvent.done(AssistantMessage.text("never"))

    r = await Agent(provider=Waiting(), limits=RunLimits(run_timeout=0.01)).prompt("go")
    assert r.status == "limit_reached" and r.stop_reason == "run_timeout"


async def test_C21_tool_timeout_is_an_error_result():
    async def slow(args, ctx):
        await asyncio.Event().wait()

    p = ScriptedProvider([batch("t"), AssistantMessage.text("handled")])
    r = await Agent(
        provider=p, tools=[make_tool("t", slow)], limits=RunLimits(tool_timeout=0.01)
    ).prompt("go")
    assert r.status == "completed" and not r.reconciliation_required and r.cleanup_complete
    assert r.tool_outcomes[0].execution_status == "cancelled"
    assert r.tool_outcomes[0].result.error_code == "tool_timeout"
    assert len(p.requests) == 2  # the model sees the error and answers


async def test_C21_no_default_request_or_tool_budget():
    calls = [batch(str(i)) for i in range(40)]
    tools = [make_tool(str(i), lambda args, ctx: ToolResult.text("ok")) for i in range(40)]
    p = ScriptedProvider([*calls, AssistantMessage.text("done")])
    r = await Agent(provider=p, tools=tools).prompt("go")
    assert r.status == "completed" and len(p.requests) == 41
    limits = RunLimits()
    assert (limits.max_model_requests, limits.max_tool_calls, limits.max_concurrency) == (
        None,
        None,
        None,
    )


async def test_C21_uncooperative_cleanup_poisoned_not_idle():
    entered = asyncio.Event()
    release = asyncio.Event()
    exited = asyncio.Event()

    async def stubborn(args, ctx):
        entered.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return ToolResult.text("late")
        finally:
            exited.set()

    a = Agent(
        provider=ScriptedProvider([batch("t")]),
        tools=[make_tool("t", stubborn)],
        limits=RunLimits(cleanup_timeout=0.01),
    )
    task = asyncio.create_task(a.prompt("go"))
    await entered.wait()
    a.abort()
    try:
        r = await task
        assert r.status == "cancelled" and r.stop_reason == "tool_not_stopped"
        assert not r.cleanup_complete and a.state.is_running
        with pytest.raises(CleanupTimeoutError):
            await a.wait_for_idle()
        with pytest.raises(CleanupTimeoutError):
            await a.aclose()
        assert r.tool_outcomes[0].execution_status == "running"
        assert r.tool_outcomes[0].result.error_code == "not_stopped"
    finally:
        release.set()
    await exited.wait()
    await asyncio.sleep(0)
    assert r.tool_outcomes[0].execution_status == "running"  # detached result is stable


async def test_C21_bounded_parallelism():
    entered = asyncio.Event()
    release = asyncio.Event()
    active = peak = 0

    async def execute(args, ctx):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1
        return ToolResult.text("ok")

    tools = [make_tool(str(i), execute) for i in range(6)]
    a = Agent(
        provider=ScriptedProvider([batch(*[t.name for t in tools]), AssistantMessage.text("done")]),
        tools=tools,
        limits=RunLimits(max_concurrency=2),
    )
    task = asyncio.create_task(a.prompt("go"))
    await entered.wait()
    release.set()
    await task
    assert peak == 2


async def test_C21_parallel_batch_is_unbounded_by_default():
    release = asyncio.Event()
    started = []

    async def execute(args, ctx):
        started.append(ctx.call_id)
        if len(started) == 6:
            release.set()
        await release.wait()
        return ToolResult.text("ok")

    tools = [make_tool(str(i), execute) for i in range(6)]
    p = ScriptedProvider([batch(*[t.name for t in tools]), AssistantMessage.text("done")])
    r = await asyncio.wait_for(Agent(provider=p, tools=tools).prompt("go"), 5)
    assert r.status == "completed" and len(started) == 6  # all six ran at once, as in Pi


async def test_cancel_before_driver_starts():
    a = Agent(provider=ScriptedProvider([AssistantMessage.text("unused")]))
    task = asyncio.create_task(a.prompt("go"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await a.wait_for_idle()
    assert not a.state.is_running


async def test_event_mutation_cannot_change_other_listener():
    first = []
    second = []

    def mutate(e):
        e.data.clear()
        first.append(e)

    a = Agent(provider=ScriptedProvider([AssistantMessage.text("done")]))
    a.subscribe(mutate)
    a.subscribe(second.append)
    await a.prompt("go")
    assert any(e.type == "message_end" and e.data for e in second)


async def test_event_queue_only_drops_deltas():
    q = EventQueue(1, drop_text_updates=True)
    await q(Event("agent_start", "r", 1, 1))
    await q(Event("message_update", "r", 1, 2))
    assert q.dropped_updates == 1
    task = asyncio.create_task(q(Event("tool_execution_end", "r", 1, 3)))
    await asyncio.sleep(0)
    assert not task.done()
    await q.get()
    await task
    assert (await q.get()).type == "tool_execution_end"


@pytest.mark.parametrize("external", [False, True])
async def test_cancel_during_end_listener_is_bounded(external):
    entered = asyncio.Event()

    async def listener(e):
        if e.type == "agent_end":
            entered.set()
            await asyncio.Event().wait()

    a = Agent(
        provider=ScriptedProvider([AssistantMessage.text("done")]),
        limits=RunLimits(cleanup_timeout=0.05),
    )
    a.subscribe(listener)
    task = asyncio.create_task(a.prompt("go"))
    await entered.wait()
    if external:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    else:
        a.abort()
        r = await asyncio.wait_for(task, 1)
        assert r.status == "cancelled"
    await a.wait_for_idle()


async def test_cancel_during_after_hook_keeps_success():
    entered = asyncio.Event()

    async def execute(args, ctx):
        return ToolResult.text("success")

    async def after(call, result, ctx):
        entered.set()
        await asyncio.Event().wait()

    a = Agent(
        provider=ScriptedProvider([batch("t")]),
        tools=[make_tool("t", execute)],
        hooks=Hooks(after_tool_call=after),
    )
    task = asyncio.create_task(a.prompt("go"))
    await entered.wait()
    a.abort()
    r = await task
    assert r.tool_outcomes[0].execution_status == "succeeded"
    assert r.tool_outcomes[0].raw_result.content[0].text == "success"
    assert not r.reconciliation_required and r.tool_outcomes[0].result.is_error


async def test_continue_queued_final_starts_with_input_not_empty_request():
    p = ScriptedProvider(
        [
            AssistantMessage.text("first"),
            AssistantMessage.text("second"),
            AssistantMessage.text("third"),
        ]
    )
    a = Agent(provider=p)
    await a.prompt("go")
    a.follow_up("queued")
    await a.continue_run()
    assert len(p.requests) == 2 and p.requests[1].messages[-1].content == "queued"
    b = Agent(provider=ScriptedProvider([]), system_prompt="only system")
    with pytest.raises(InvalidContinuationError):
        await b.continue_run()


async def test_cancel_during_preparation_restores_selected_queue():
    entered = asyncio.Event()

    async def prepare(ctx, cancel):
        entered.set()
        await asyncio.Event().wait()

    async def execute(args, ctx):
        return ToolResult.text("ok")

    a = Agent(
        provider=ScriptedProvider([batch("t")]),
        tools=[make_tool("t", execute)],
        hooks=Hooks(prepare_next_turn=prepare),
    )
    steered = []

    def steer_once(e):
        if e.type == "turn_end" and not steered:
            steered.append(1)
            a.steer("keep me")

    a.subscribe(steer_once)
    task = asyncio.create_task(a.prompt("go"))
    await entered.wait()
    a.abort()
    r = await task
    assert r.queued_steering == 1
    assert not any(isinstance(m, UserMessage) and m.content == "keep me" for m in r.messages)


async def test_late_success_cannot_overwrite_timeout_decision():
    release = asyncio.Event()

    async def execute(args, ctx):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            return ToolResult.text("late success")

    a = Agent(
        provider=ScriptedProvider([batch("t"), AssistantMessage.text("handled")]),
        tools=[make_tool("t", execute)],
        limits=RunLimits(tool_timeout=0.01),
    )
    a.subscribe(lambda e: release.set() if e.type == "tool_execution_end" else None)
    r = await a.prompt("go")
    assert not r.reconciliation_required
    assert r.tool_outcomes[0].execution_status == "cancelled"
    assert r.tool_outcomes[0].result.error_code == "tool_timeout"
    assert next(m for m in r.messages if isinstance(m, ToolResultMessage)).is_error


async def test_C23_no_implicit_environment_configuration(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-core")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "also-not-for-core")
    p = ScriptedProvider([AssistantMessage.text("done")])
    await Agent(provider=p).prompt("go")
    assert p.requests[0].options == {} and p.requests[0].model == "mock"


async def test_stuck_sync_tool_lets_the_agent_recover_once_it_finishes():
    import threading
    import time

    started = threading.Event()

    def blocking(args, ctx):
        started.set()
        time.sleep(0.3)  # a thread cannot be interrupted
        return "late"

    p = ScriptedProvider([batch("t"), AssistantMessage.text("again")])
    a = Agent(provider=p, tools=[make_tool("t", blocking)], limits=RunLimits(cleanup_timeout=0.05))
    task = asyncio.create_task(a.prompt("go"))
    while not started.is_set():
        await asyncio.sleep(0.01)
    a.abort()
    r = await task
    assert r.stop_reason == "tool_not_stopped" and not r.cleanup_complete
    with pytest.raises(CleanupTimeoutError):
        await a.prompt("too early")
    await asyncio.sleep(0.4)  # the thread has finished; nothing the agent owns is running
    assert a.state.cleanup_complete and not a.state.is_running
    await a.wait_for_idle()
    assert (await a.prompt("again")).status == "completed"


async def test_abort_from_another_thread_takes_effect_promptly():
    import threading
    import time

    class Slow:
        async def stream(self, request, cancel):
            await asyncio.sleep(5)
            yield ModelEvent.done(AssistantMessage.text("never"))

    a = Agent(provider=Slow())
    threading.Timer(0.05, a.abort).start()
    started = time.monotonic()
    r = await a.prompt("go")
    assert r.status == "cancelled" and time.monotonic() - started < 2


async def test_missing_provider_is_a_configuration_error():
    with pytest.raises(ConfigurationError, match="provider"):
        await Agent().prompt("go")
