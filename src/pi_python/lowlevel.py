"""Public low-level loop variants backed by the same Agent execution engine."""

from __future__ import annotations
import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from .agent import Agent
from .cancellation import CancelToken
from .events import Event, EventListener
from .hooks import Hooks
from .limits import RunLimits
from .messages import Message
from .models import ModelInfo
from .provider import Provider
from .tools import Tool


@dataclass
class AgentContext:
    messages: list[Message] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)
    system_prompt: str = ""


@dataclass
class AgentLoopConfig:
    provider: Provider | None = None
    stream_fn: Any = None
    model: str | ModelInfo = "mock"
    options: dict[str, Any] = field(default_factory=dict)
    hooks: Hooks = field(default_factory=Hooks)
    limits: RunLimits = field(default_factory=RunLimits)
    tool_execution: str = "parallel"
    get_steering_messages: Any = None
    get_follow_up_messages: Any = None


async def _run(
    prompts: list[Message] | None,
    context: AgentContext,
    config: AgentLoopConfig,
    emit: EventListener | None,
    cancel: CancelToken | None,
    continuing: bool,
) -> list[Message]:
    agent = Agent(
        provider=config.provider,
        stream_fn=config.stream_fn,
        model=config.model,
        options=config.options,
        messages=context.messages,
        tools=context.tools,
        system_prompt=context.system_prompt,
        hooks=config.hooks,
        limits=config.limits,
        execution_mode=config.tool_execution,
    )
    agent._get_steering_messages = config.get_steering_messages
    agent._get_follow_up_messages = config.get_follow_up_messages
    if emit is not None:
        agent.subscribe(emit)

    async def watch(token: CancelToken) -> None:
        await token.wait()
        agent.abort(token.reason or "requested")

    watcher = asyncio.create_task(watch(cancel)) if cancel else None
    try:
        if cancel and cancel.cancelled:
            return []
        result = await (agent.continue_run() if continuing else agent.prompt(prompts or []))
        if continuing:
            context.messages[:] = list(agent.state.messages)
        return result.messages
    finally:
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


async def run_agent_loop(
    prompts: list[Message],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: EventListener | None = None,
    cancel: CancelToken | None = None,
) -> list[Message]:
    return await _run(prompts, context, config, emit, cancel, False)


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: EventListener | None = None,
    cancel: CancelToken | None = None,
) -> list[Message]:
    return await _run(None, context, config, emit, cancel, True)


class AgentEventStream:
    """Consume via async for, or await result() to drain events and obtain messages.

    The bounded queue applies backpressure. Close an abandoned stream explicitly.
    """

    def __init__(
        self, runner: Callable[[Callable], Awaitable[list[Message]]], *, maxsize: int = 128
    ) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize)
        self._iterating = False
        self._ended = False

        async def emit(event: Event) -> None:
            await self.queue.put(deepcopy(event))

        async def drive() -> list[Message]:
            try:
                return await runner(emit)
            finally:
                await self.queue.put(None)

        self.task = asyncio.create_task(drive())

    def __aiter__(self) -> AgentEventStream:
        self._iterating = True
        return self

    async def __anext__(self) -> Event:
        if self._ended:
            raise StopAsyncIteration
        if self.task.done() and self.queue.empty():
            self._ended = True
            await self.task
            raise StopAsyncIteration
        event = await self.queue.get()
        if event is None:
            self._ended = True
            await self.task
            raise StopAsyncIteration
        return event

    async def result(self) -> list[Message]:
        if self.task.done():
            return await self.task
        if not self._iterating:
            async for _ in self:
                pass
        return await asyncio.shield(self.task)

    async def aclose(self) -> None:
        self.task.cancel()

        # Drain so a cancelled producer cannot block on its terminal marker.
        async def drain() -> None:
            while not self.task.done():
                try:
                    await asyncio.wait_for(self.queue.get(), 0.05)
                except TimeoutError:
                    pass

        await drain()
        await asyncio.gather(self.task, return_exceptions=True)


def agent_loop(
    prompts: list[Message],
    context: AgentContext,
    config: AgentLoopConfig,
    cancel: CancelToken | None = None,
) -> AgentEventStream:
    return AgentEventStream(lambda emit: run_agent_loop(prompts, context, config, emit, cancel))


def agent_loop_continue(
    context: AgentContext, config: AgentLoopConfig, cancel: CancelToken | None = None
) -> AgentEventStream:
    return AgentEventStream(lambda emit: run_agent_loop_continue(context, config, emit, cancel))
