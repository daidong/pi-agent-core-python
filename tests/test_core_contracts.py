"""Canonical contracts and research conveniences share one execution engine."""

import asyncio
from dataclasses import replace

import httpx
import pytest

from pi_python import (
    Agent as ResearchAgent,
    AssistantMessage,
    CancelToken,
    Hooks as LegacyHooks,
    InvalidContinuationError,
    ModelEvent,
    ModelRequest,
    ScriptedProvider,
    TextContent,
    ToolCall,
    ToolResult,
    UserMessage,
)
from pi_python import core
from pi_python.providers import HTTPTransport, OpenAICompletionsProvider
from tests.test_providers import remote, events
from tests.test_completions import sse, chunk


async def test_core_pre_cancelled_lifecycle_and_strict_continue():
    cancel = CancelToken()
    cancel.cancel()
    provider = ScriptedProvider([AssistantMessage.text("unused")])
    seen = []
    result = await core.run_agent_loop(
        [UserMessage("go")],
        core.AgentContext(),
        core.AgentLoopConfig(provider=provider),
        seen.append,
        cancel,
    )
    assert [e["type"] for e in seen] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert result[-1].stop_reason == "aborted"
    assert not provider.requests
    with pytest.raises(InvalidContinuationError):
        await core.run_agent_loop_continue(
            core.AgentContext(result), core.AgentLoopConfig(provider=provider)
        )


async def test_core_full_tool_events_and_partial_hook_overrides():
    seen, callbacks, updates = [], [], []

    async def execute(call_id, args, signal, on_update):
        callbacks.append((call_id, args, signal))
        updates.append(on_update)
        await on_update(ToolResult.text("working"))
        return ToolResult.text("original", details={"old": True}, structured_content={"value": 1})

    def before(context, signal):
        assert context.tool_call.id == "c"
        assert context.args == {"x": 1}
        assert context.assistant_message.tool_calls[0].name == "t"
        assert isinstance(context.context, core.AgentContext)
        return {"block": False}

    def after(context, signal):
        assert context.result.details == {"old": True}
        # Explicit null clears; omitted values preserve. Content clears stale structured data.
        return {"content": [{"type": "text", "text": "changed"}], "details": None}

    agent = core.Agent(
        provider=ScriptedProvider([AssistantMessage([ToolCall("c", "t", {"x": 1})], "tool_use")]),
        tools=[core.Tool("t", "", {"type": "object"}, execute)],
        before_tool_call=before,
        after_tool_call=after,
        finish_turn=lambda context, signal: {"action": "end"},
    )
    agent.subscribe(lambda event, signal: seen.append(event))
    assert await agent.prompt("go") is None
    assert agent.state.error_message is None
    start = next(e for e in seen if e["type"] == "tool_execution_start")
    update = next(e for e in seen if e["type"] == "tool_execution_update")
    end = next(e for e in seen if e["type"] == "tool_execution_end")
    assert start == {
        "type": "tool_execution_start",
        "tool_call_id": "c",
        "tool_name": "t",
        "args": {"x": 1},
    }
    assert update["args"] == {"x": 1} and update["tool_name"] == "t"
    assert update["partial_result"]["content"][0]["text"] == "working"
    assert end["tool_name"] == "t" and not end["is_error"]
    assert end["result"]["content"][0]["text"] == "changed"
    assert end["result"]["details"] is None and end["result"]["structured_content"] is None
    assert set(seen[-1]) == {"type", "messages"}
    assert seen[-1]["messages"][-1]["role"] == "tool_result"
    size = len(seen)
    await updates[0](ToolResult.text("too late"))
    assert len(seen) == size


async def test_core_message_update_contains_original_assistant_event():
    provider = ScriptedProvider(
        [
            [
                ModelEvent.boundary("start", 0, TextContent("")),
                ModelEvent.text("hello"),
                ModelEvent.boundary("end", 0, TextContent("hello")),
                ModelEvent.done(AssistantMessage.text("hello")),
            ]
        ]
    )
    agent = core.Agent(provider=provider)
    seen = []
    agent.subscribe(lambda e, signal: seen.append(e))
    await agent.prompt("go")
    delta = next(
        e for e in seen if e.get("assistant_message_event", {}).get("type") == "text_delta"
    )
    assert delta["assistant_message_event"]["content_index"] == 0
    assert delta["assistant_message_event"]["delta"] == "hello"
    assert delta["message"] == delta["assistant_message_event"]["partial"]


async def test_prepare_request_and_next_turn_have_separate_contracts():
    seen = []
    provider = ScriptedProvider([AssistantMessage.text("one"), AssistantMessage.text("two")])

    def prepare(context, signal):
        seen.append(context)
        assert isinstance(context, core.PrepareRequestContext)
        return core.AgentRequestUpdate(thinking_level="low")

    def next_turn(context, signal):
        assert isinstance(context, core.AgentTurnContext)
        return core.AgentLoopTurnUpdate(messages=[UserMessage("follow")])

    agent = core.Agent(
        provider=provider,
        prepare_request=prepare,
        prepare_next_turn_with_context=next_turn,
        finish_turn=lambda c, s: {"action": "continue" if len(seen) == 1 else "end"},
    )
    await agent.prompt("go")
    assert agent.state.error_message is None
    assert len(provider.requests) == 2
    assert all(r.options["reasoning"] == "low" for r in provider.requests)
    assert provider.requests[1].messages[-1].content == "follow"
    bad = core.Agent(
        provider=ScriptedProvider([]),
        prepare_request=lambda c, s: {"messages": [UserMessage("illegal")]},
    )
    await bad.prompt("go")
    assert bad.state.error_message is not None


async def test_research_conveniences_do_not_leak_into_core():
    class Provider(ScriptedProvider):
        supports_agent_cache_session = True

    a, b = Provider([AssistantMessage.text("ok")]), Provider([AssistantMessage.text("ok")])
    research = ResearchAgent(provider=a, hooks=LegacyHooks(finish_turn=lambda *_: "end"))
    basic = core.Agent(provider=b)
    assert (await research.prompt("go")).status == "completed"
    await basic.prompt("go")
    assert a.requests[0].cache_session_id is not None
    assert b.requests[0].cache_session_id is None
    failed = [UserMessage("go"), AssistantMessage([], "error", error="failed")]
    assert (
        await ResearchAgent(
            provider=ScriptedProvider([AssistantMessage.text("ok")]), messages=failed
        ).continue_run()
    ).status == "completed"
    with pytest.raises(InvalidContinuationError):
        await core.Agent(provider=ScriptedProvider([]), messages=failed).continue_run()


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_model_endpoint_headers_and_model_callbacks_reach_wire(kind):
    provider, requests, _, client = remote(kind, [events(kind + "-text")])
    model = replace(
        provider.catalog.get(kind, "test-model"),
        base_url="https://model.test/v2",
        headers={"X-Model": "yes", "X-Override": "model"},
    )
    callbacks = []

    def observe(value, current_model):
        callbacks.append(current_model)

    request = ModelRequest(
        [UserMessage("go")],
        model=model.id,
        model_info=model,
        options={"headers": {"X-Override": "request", "Accept": None, "Authorization": "custom"}},
        on_payload=observe,
        on_response=observe,
        on_provider_stream_event=observe,
    )
    async with client:
        output = [e async for e in provider.stream(request, CancelToken())]
    assert output[-1].type == "done", output[-1]
    assert str(requests[0].url).startswith("https://model.test/v2/")
    assert requests[0].headers["x-model"] == "yes"
    assert requests[0].headers["x-override"] == "request"
    assert requests[0].headers["authorization"] == "custom"
    assert "accept" not in requests[0].headers
    assert all(m == model for m in callbacks) and len(callbacks) >= 3
    assert provider.base_url != model.base_url
    assert request.options["headers"]["X-Override"] == "request"


async def test_sampling_inheritance_and_request_retry_options():
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, content=sse([chunk({"content": "ok"}), chunk(finish="stop")]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = HTTPTransport(client, max_retries=0)
        provider = OpenAICompletionsProvider(
            base_url="https://fallback.test", api_key="test", transport=transport
        )
        model = replace(
            provider.model("test"),
            reasoning=True,
            sampling_params={"top_p": 0.9, "temperature": 0.7},
            sampling_params_by_thinking_level={"low": {"temperature": 0.4}},
        )
        request = ModelRequest(
            [UserMessage("go")],
            model="test",
            model_info=model,
            options={
                "reasoning": "low",
                "sampling_params": {"top_p": 0.8},
                "max_retries": 1,
                "timeout_ms": 1250,
            },
        )
        output = [e async for e in provider.stream(request, CancelToken())]
    assert output[-1].type == "done", output[-1]
    assert len(requests) == 2 and transport.max_retries == 0
    import json

    body = json.loads(requests[-1].content)
    assert body["top_p"] == 0.8 and body["temperature"] == 0.4
    assert requests[-1].extensions["timeout"]["read"] == 1.25


async def test_callback_type_error_is_not_retried_with_old_signature():
    provider, requests, _, client = remote("openai", [])
    calls = []

    def callback(body, model):
        calls.append(model)
        raise TypeError("from callback body")

    async with client:
        output = [
            e
            async for e in provider.stream(
                ModelRequest([UserMessage("go")], model="test-model", on_payload=callback),
                CancelToken(),
            )
        ]
    assert output[-1].type == "error"
    assert len(calls) == 1 and not requests


async def test_core_stream_result_preserves_events_without_consumption():
    stream = core.agent_loop(
        [UserMessage("go")],
        core.AgentContext(),
        core.AgentLoopConfig(
            provider=ScriptedProvider([AssistantMessage.text("ok")]),
            finish_turn=lambda c, s: {"action": "end"},
        ),
    )
    result = await asyncio.wait_for(stream.result(), 1)
    seen = [e async for e in stream]
    assert seen[-1]["messages"][-1]["content"][0]["text"] == "ok"
    assert result[-1].content[0].text == "ok"


async def test_core_stream_fn_uses_model_context_options_and_closes():
    calls, closed = [], []

    async def stream(model, context, options):
        calls.append((model, context, options))
        try:
            yield ModelEvent.done(AssistantMessage.text("ok"))
        finally:
            closed.append(True)

    agent = core.Agent(stream_fn=stream, model="test", session_id="explicit")
    await agent.prompt("go")
    assert agent.state.error_message is None
    assert calls[0][0] == "test" and isinstance(calls[0][1], core.AgentContext)
    assert calls[0][2]["session_id"] == "explicit"
    assert isinstance(calls[0][2]["signal"], CancelToken)
    assert closed == [True]


async def test_concurrent_model_requests_do_not_leak_endpoint_or_headers():
    captured, ready = [], asyncio.Event()

    async def respond(request):
        captured.append(request)
        if len(captured) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 1)
        return httpx.Response(200, content=sse([chunk({"content": "ok"}), chunk(finish="stop")]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompletionsProvider(
            base_url="https://fallback.test", transport=HTTPTransport(client)
        )

        async def run(name):
            model = replace(
                provider.model(name), base_url=f"https://{name}.test", headers={"x-owner": name}
            )
            return [
                e
                async for e in provider.stream(
                    ModelRequest([UserMessage("go")], model=name, model_info=model), CancelToken()
                )
            ]

        results = await asyncio.gather(run("one"), run("two"))
    assert all(r[-1].type == "done" for r in results)
    assert {(r.url.host, r.headers["x-owner"]) for r in captured} == {
        ("one.test", "one"),
        ("two.test", "two"),
    }
    assert provider.base_url == "https://fallback.test"


async def test_request_timeout_is_enforced_on_real_local_socket():
    tasks = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.Event().wait()
        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    provider = OpenAICompletionsProvider(base_url=f"http://127.0.0.1:{port}")
    try:
        model = provider.model("local")
        request = ModelRequest(
            [UserMessage("go")],
            model="local",
            model_info=model,
            options={"timeout_ms": 30, "max_retries": 0},
        )
        output = await asyncio.wait_for(_collect(provider, request), 2)
        assert output[-1].type == "error"
        assert "ReadTimeout" in output[-1].message.error
    finally:
        await provider.aclose()
        server.close()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()


async def _collect(provider, request):
    return [event async for event in provider.stream(request, CancelToken())]
