"""Result completion must not depend on draining a low-level event stream."""

import asyncio

import pytest

from pi_python import (
    AgentContext,
    AgentLoopConfig,
    AssistantMessage,
    Event,
    ModelEvent,
    TextContent,
    UserMessage,
    agent_loop,
)
from pi_python.lowlevel import AgentEventStream


@pytest.mark.parametrize("consumed", [0, 1, 3])
async def test_result_retains_all_unread_events_after_releasing_backpressure(consumed):
    messages = [AssistantMessage.text("done")]

    async def runner(emit):
        for i in range(200):
            await emit(Event("probe", "run", 0, i + 1))
        return messages

    stream = AgentEventStream(runner, maxsize=2)
    iterator = stream.__aiter__()
    try:
        before = [await anext(iterator) for _ in range(consumed)]
        result = await asyncio.wait_for(stream.result(), 1)
        remaining = [event async for event in iterator]
        assert result == messages
        assert [e.sequence for e in before + remaining] == list(range(1, 201))
        assert await stream.result() == messages
    finally:
        await stream.aclose()


async def test_iterator_only_keeps_backpressure_and_close_unblocks_producer():
    attempted_second = asyncio.Event()
    emitted_second = asyncio.Event()

    async def runner(emit):
        await emit(Event("probe", "run", 0, 1))
        attempted_second.set()
        await emit(Event("probe", "run", 0, 2))
        emitted_second.set()
        return []

    stream = AgentEventStream(runner, maxsize=1)
    try:
        await asyncio.wait_for(attempted_second.wait(), 1)
        assert not emitted_second.is_set()
        assert not stream.task.done()
        await asyncio.wait_for(stream.aclose(), 1)
        assert stream.task.done()
        assert not emitted_second.is_set()
    finally:
        await stream.aclose()


async def test_cancelled_result_waiter_does_not_cancel_stream_or_lose_events():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def runner(emit):
        await emit(Event("probe", "run", 0, 1))
        entered.set()
        await release.wait()
        await emit(Event("probe", "run", 0, 2))
        return []

    stream = AgentEventStream(runner, maxsize=1)
    waiter = asyncio.create_task(stream.result())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not stream.task.done()
        release.set()
        assert await asyncio.wait_for(stream.result(), 1) == []
        assert [e.sequence async for e in stream] == [1, 2]
    finally:
        await stream.aclose()


async def test_error_result_keeps_prior_events_and_propagates_failure():
    async def runner(emit):
        for i in range(3):
            await emit(Event("probe", "run", 0, i + 1))
        raise ValueError("runner failure")

    stream = AgentEventStream(runner, maxsize=1)
    try:
        with pytest.raises(ValueError, match="runner failure"):
            await asyncio.wait_for(stream.result(), 1)
        iterator = stream.__aiter__()
        assert [(await anext(iterator)).sequence for _ in range(3)] == [1, 2, 3]
        with pytest.raises(ValueError, match="runner failure"):
            await anext(iterator)
    finally:
        await stream.aclose()


async def test_actual_agent_loop_can_finish_after_one_event_then_replay_the_rest():
    async def provider(request, cancel):
        yield ModelEvent.boundary("start", 0, TextContent(""))
        for _ in range(200):
            yield ModelEvent.text("x")
        yield ModelEvent.boundary("end", 0, TextContent("x" * 200))
        yield ModelEvent.done(AssistantMessage.text("x" * 200))

    stream = agent_loop([UserMessage("go")], AgentContext(), AgentLoopConfig(stream_fn=provider))
    try:
        iterator = stream.__aiter__()
        first = await anext(iterator)
        result = await asyncio.wait_for(stream.result(), 2)
        events = [first, *[event async for event in iterator]]
        assert result[-1].content == [TextContent("x" * 200)]
        assert events[0].type == "agent_start"
        assert events[-1].type == "agent_end"
        assert len(events) > 128
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    finally:
        await stream.aclose()
