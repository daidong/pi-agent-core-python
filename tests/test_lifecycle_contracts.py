"""Ownership contracts across cancellation, startup and shutdown boundaries."""

import asyncio
import warnings

import httpx
import pytest

from pi_python import AssistantMessage, CancelToken, ModelEvent, ScriptedProvider, ToolCall
from pi_python.plugins import AgentDefinition, Plugin, PluginWarning, load_plugins
from pi_python.providers import HTTPTransport


@pytest.mark.parametrize("cancellation", ["token", "caller"])
async def test_response_acquired_during_cancellation_is_closed(cancellation):
    token = CancelToken()
    owner = asyncio.current_task()

    class Body(httpx.AsyncByteStream):
        closes = 0

        async def __aiter__(self):
            yield b'data: {"type":"done"}\n\n'

        async def aclose(self):
            self.closes += 1

    body = Body()

    async def handler(request):
        if cancellation == "token":
            token.cancel()
        else:
            owner.cancel()
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HTTPTransport(client)
        stream = transport.stream("https://test.invalid", {}, {}, token)
        try:
            with pytest.raises(asyncio.CancelledError):
                await anext(stream)
        finally:
            await stream.aclose()
        assert body.closes == 1
        assert not client.is_closed  # borrowed client remains application-owned


async def test_parallel_subagent_failure_joins_siblings_before_parent_finishes():
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []

    class Slow:
        async def stream(self, request, cancel):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish.wait()
                cleaned.append(True)
            yield ModelEvent.done(AssistantMessage.text("unused"))

    class Fast:
        async def stream(self, request, cancel):
            await started.wait()
            yield ModelEvent.done(AssistantMessage.text("done"))

    def setup(api):
        api.add_agent(AgentDefinition("slow", "Slow", provider=Slow(), tools=[]))
        api.add_agent(AgentDefinition("fast", "Fast", provider=Fast(), tools=[]))

    async with load_plugins(Plugin("team", setup)) as plugins:
        agent = plugins.agent(
            provider=ScriptedProvider(
                [
                    AssistantMessage(
                        [
                            ToolCall(
                                "c",
                                "subagent",
                                {
                                    "tasks": [
                                        {"agent": "slow", "task": "work"},
                                        {"agent": "fast", "task": "work"},
                                    ]
                                },
                            )
                        ],
                        "tool_use",
                    ),
                ]
            )
        )

        def listener(event):
            if event.type == "tool_execution_update":
                raise RuntimeError("recorder failed")

        agent.subscribe(listener)
        running = asyncio.create_task(agent.prompt("go"))
        try:
            await asyncio.wait_for(cleaning.wait(), 2)
            for _ in range(20):
                await asyncio.sleep(0)
            assert not running.done()
        finally:
            finish.set()
            result = await running
            for _ in range(20):
                await asyncio.sleep(0)
        assert cleaned == [True]
        assert result.status == "failed" and result.cleanup_complete


@pytest.mark.parametrize("close_caller_cancelled", [False, True])
async def test_close_during_plugin_setup_joins_setup_and_releases_all_resources(
    close_caller_cancelled,
):
    entered, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed = []

    async def setup(api):
        api.on_close(lambda: closed.append("early"))
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()
            api.on_close(lambda: closed.append("late"))

    plugins = load_plugins(Plugin("starting", setup))
    opening = asyncio.create_task(plugins.open())
    await entered.wait()
    closing = asyncio.create_task(plugins.aclose())
    try:
        # Baseline doesn't cancel setup: bound the assertion, then clean it up below.
        await asyncio.wait_for(cleaning.wait(), 0.5)
        if close_caller_cancelled:
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
        assert not closing.done()
    finally:
        opening.cancel()
        finish.set()
        await asyncio.gather(opening, closing, return_exceptions=True)
        await plugins.aclose()
    assert closed == ["late", "early"]
    assert not (await plugins.readiness()).loaded


async def test_warning_as_error_rolls_back_plugin_startup(tmp_path):
    skill = tmp_path / "skills" / "invalid" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: invalid\n---\nMissing description", encoding="utf-8")
    closed = []
    plugins = load_plugins(
        Plugin("warning", lambda api: api.on_close(lambda: closed.append(True)), root=tmp_path)
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PluginWarning)
            with pytest.raises(PluginWarning):
                async with plugins:
                    pytest.fail("entry should fail")
        assert closed == [True]
        assert not (await plugins.readiness()).loaded
    finally:
        await plugins.aclose()


@pytest.mark.parametrize("transport_kind", ["http", "websocket"])
async def test_cancelled_acquisition_joins_release_despite_repeated_cancellation(
    transport_kind, monkeypatch
):
    import websockets.asyncio.client

    token = CancelToken()
    cleaning, finish = asyncio.Event(), asyncio.Event()
    closed = []

    class Resource(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"unused"

        async def aclose(self):
            cleaning.set()
            await finish.wait()
            closed.append(True)

        close = aclose

    resource = Resource()

    async def connect(*args, **kwargs):
        token.cancel()
        return resource

    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)

    async def handler(request):
        token.cancel()
        return httpx.Response(200, stream=resource)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HTTPTransport(client)
        stream = (transport.stream if transport_kind == "http" else transport.websocket)(
            "https://test.invalid", {}, {}, token
        )
        caller = asyncio.create_task(anext(stream))
        try:
            await asyncio.wait_for(cleaning.wait(), 2)
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done() and not closed
        finally:
            finish.set()
            await asyncio.gather(caller, return_exceptions=True)
            await stream.aclose()
            await transport.aclose()
        assert closed == [True]


async def test_failed_plugin_setup_releases_resources_registered_in_finally():
    closed = []

    async def setup(api):
        api.on_close(lambda: closed.append("early"))
        try:
            raise ValueError("setup failed")
        finally:
            await asyncio.sleep(0)
            api.on_close(lambda: closed.append("late"))

    plugins = load_plugins(Plugin("failed", setup))
    with pytest.raises(ValueError, match="setup failed"):
        await plugins.open()
    await plugins.aclose()
    assert closed == ["late", "early"]


async def test_setup_cannot_close_its_own_plugin_set():
    from pi_python import ConfigurationError

    closed = []

    async def setup(api):
        api.on_close(lambda: closed.append(True))
        await plugins.aclose()

    plugins = load_plugins(Plugin("recursive", setup))
    with pytest.raises(ConfigurationError, match="own PluginSet"):
        await asyncio.wait_for(plugins.open(), 2)
    assert closed == [True]


async def test_cancelled_error_reporter_does_not_skip_remaining_plugin_cleanup():
    closed = []

    async def report(failure):
        raise asyncio.CancelledError

    def fail():
        raise ValueError("close failed")

    def setup(api):
        api.on_close(lambda: closed.append(True))
        api.on_close(fail)

    plugins = await load_plugins(Plugin("cleanup", setup), on_error=report).open()
    with pytest.raises(asyncio.CancelledError):
        await plugins.aclose()
    await plugins.aclose()
    assert closed == [True]
