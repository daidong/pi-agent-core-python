"""Nested agents preserve the caller's recovery and resource ownership contracts."""

import asyncio
import threading

import pytest

from pi_python import (
    Agent,
    AgentBusyError,
    AssistantMessage,
    CancelToken,
    CleanupTimeoutError,
    ModelEvent,
    RunLimits,
    ScriptedProvider,
    ToolCall,
    ToolContext,
    ToolOutcomeUnknownError,
    tool,
)
from pi_python.plugins import AgentDefinition, Plugin, load_plugins


def delegation(mode, *names):
    steps = [{"agent": name, "task": "work"} for name in names]
    return steps[0] if mode == "single" else {mode: steps}


def call(name, arguments=None):
    return AssistantMessage([ToolCall("call", name, arguments or {})], "tool_use")


@pytest.mark.parametrize("mode", ["single", "chain", "tasks"])
async def test_subagent_unknown_outcome_stops_parent_and_prevents_reuse(mode):
    @tool
    async def submit() -> str:
        raise ToolOutcomeUnknownError("Submission accepted; confirmation lost")

    child = ScriptedProvider([call("submit")])
    unused = ScriptedProvider([AssistantMessage.text("must not run")])

    def setup(api):
        api.add_tool(submit)
        api.add_agent(AgentDefinition("worker", "Worker", provider=child, tools=["submit"]))
        api.add_agent(AgentDefinition("next", "Next", provider=unused, tools=[]))

    args = delegation(mode, "worker", *(["next"] if mode == "chain" else []))
    parent_provider = ScriptedProvider([call("subagent", args), AssistantMessage.text("unsafe")])
    async with load_plugins(Plugin("team", setup)) as plugins:
        async with plugins.agent(provider=parent_provider) as parent:
            result = await parent.prompt("go")
            assert result.status == "failed"
            assert result.reconciliation_required and parent.state.reconciliation_required
            assert result.tool_outcomes[0].execution_status == "unknown"
            assert len(parent_provider.requests) == 1
            assert not unused.requests
            with pytest.raises(ToolOutcomeUnknownError):
                await parent.prompt("retry")
            with pytest.raises(ToolOutcomeUnknownError):
                await parent.continue_run()


@pytest.mark.parametrize("mode", ["single", "chain", "tasks"])
@pytest.mark.parametrize("cancellation", ["abort", "caller", "timeout"])
@pytest.mark.parametrize("blocking", [False, True], ids=["async", "thread"])
async def test_subagent_cleanup_remains_owned_until_tool_really_stops(mode, cancellation, blocking):
    started = asyncio.Event()
    exited = asyncio.Event()
    release = threading.Event() if blocking else asyncio.Event()
    loop = asyncio.get_running_loop()
    if blocking:

        @tool
        def work() -> str:
            loop.call_soon_threadsafe(started.set)
            try:
                release.wait()
                return "finished"
            finally:
                loop.call_soon_threadsafe(exited.set)

    else:

        @tool
        async def work() -> str:
            started.set()
            try:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                return "finished"
            finally:
                exited.set()

    child = ScriptedProvider([call("work")])

    def setup(api):
        api.add_tool(work)
        api.add_agent(AgentDefinition("worker", "Worker", provider=child, tools=["work"]))

    provider = ScriptedProvider(
        [call("subagent", delegation(mode, "worker")), AssistantMessage.text("next prompt")]
    )
    async with load_plugins(Plugin("team", setup)) as plugins:
        parent = plugins.agent(
            provider=provider,
            limits=RunLimits(
                cleanup_timeout=0.01,
                tool_timeout=0.1 if cancellation == "timeout" else None,
            ),
        )
        running = asyncio.create_task(parent.prompt("go"))
        try:
            await asyncio.wait_for(started.wait(), 2)
            if cancellation == "abort":
                parent.abort()
            elif cancellation == "caller":
                running.cancel()
                await asyncio.sleep(0)
                running.cancel()
            if cancellation == "caller":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(running, 2)
            else:
                result = await asyncio.wait_for(running, 2)
                assert not result.cleanup_complete
            assert parent.state.is_running and not parent.state.cleanup_complete
            assert not exited.is_set()
            assert len(provider.requests) == 1
            with pytest.raises(CleanupTimeoutError):
                await parent.prompt("too soon")
        finally:
            release.set()
            await asyncio.wait_for(exited.wait(), 2)
            await asyncio.gather(running, return_exceptions=True)
            async with asyncio.timeout(2):
                while not parent.state.cleanup_complete:
                    await asyncio.sleep(0.001)
        # Completion of descendants releases the parent's busy state automatically.
        await parent.wait_for_idle()
        assert not parent.state.is_running
        assert (await parent.prompt("safe now")).status == "completed"
        await parent.aclose()


async def test_custom_nested_tool_propagates_unknown_through_two_levels():
    children = []

    @tool
    async def submit() -> str:
        raise ToolOutcomeUnknownError("Unknown external outcome")

    @tool
    async def delegate(context: ToolContext) -> str:
        child = Agent(provider=ScriptedProvider([call("submit")]), tools=[submit])
        children.append(child)
        await context.run_agent(child, "work")
        return "must not succeed"

    @tool
    async def outer(context: ToolContext) -> str:
        child = Agent(provider=ScriptedProvider([call("delegate")]), tools=[delegate])
        children.append(child)
        await context.run_agent(child, "work")
        return "must not succeed"

    async with Agent(provider=ScriptedProvider([call("outer")]), tools=[outer]) as parent:
        result = await parent.prompt("go")
        assert result.reconciliation_required
        assert result.tool_outcomes[0].execution_status == "unknown"
        assert len(children) == 2 and all(child.state.closed for child in children)


async def test_parallel_unknown_is_preserved_while_sibling_cleanup_is_pending():
    entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @tool
    async def work() -> str:
        entered.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return "finished"
        finally:
            exited.set()

    @tool
    async def submit() -> str:
        await entered.wait()
        raise ToolOutcomeUnknownError("Unknown external outcome")

    def setup(api):
        api.add_tool(work)
        api.add_tool(submit)
        api.add_agent(
            AgentDefinition(
                "slow", "Slow", provider=ScriptedProvider([call("work")]), tools=["work"]
            )
        )
        api.add_agent(
            AgentDefinition(
                "unsafe", "Unsafe", provider=ScriptedProvider([call("submit")]), tools=["submit"]
            )
        )

    provider = ScriptedProvider([call("subagent", delegation("tasks", "slow", "unsafe"))])
    async with load_plugins(Plugin("team", setup)) as plugins:
        parent = plugins.agent(provider=provider, limits=RunLimits(cleanup_timeout=0.01))
        running = asyncio.create_task(parent.prompt("go"))
        try:
            # The unknown result cancels the sibling, whose cleanup does not finish yet.
            result = await asyncio.wait_for(running, 2)
            assert result.reconciliation_required
            assert not result.cleanup_complete
            assert parent.state.reconciliation_required
            assert len(provider.requests) == 1
        finally:
            release.set()
            await asyncio.wait_for(exited.wait(), 2)
            await asyncio.gather(running, return_exceptions=True)
            async with asyncio.timeout(2):
                while not parent.state.cleanup_complete:
                    await asyncio.sleep(0.001)
        with pytest.raises(ToolOutcomeUnknownError):
            await parent.prompt("still unsafe")
        await parent.aclose()


@pytest.mark.parametrize("failure", [False, True])
async def test_nested_helper_returns_ordinary_results_and_borrows_provider(failure):
    class Provider(ScriptedProvider):
        closed = False

        async def aclose(self):
            self.closed = True

    provider = Provider(
        [RuntimeError("model unavailable") if failure else AssistantMessage.text("ok")]
    )
    child = Agent(provider=provider)
    context = ToolContext("run", "call", CancelToken(), None)
    result = await context.run_agent(child, "work")
    assert result.status == ("failed" if failure else "completed")
    assert child.state.closed and child.state.cleanup_complete
    assert not provider.closed


async def test_nested_helper_does_not_adopt_busy_agent():
    entered, release = asyncio.Event(), asyncio.Event()

    async def stream(request, cancel):
        entered.set()
        await release.wait()
        yield ModelEvent.done(AssistantMessage.text("ok"))

    child = Agent(stream_fn=stream)
    running = asyncio.create_task(child.prompt("original owner"))
    await entered.wait()
    try:
        context = ToolContext("run", "call", CancelToken(), None)
        with pytest.raises(AgentBusyError):
            await context.run_agent(child, "other owner")
        assert not child.state.closed and not child.signal.cancelled
    finally:
        release.set()
        await running
        await child.aclose()
