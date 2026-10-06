"""Migration acceptance: real HTTP/SSE Provider parsers across process MCP links."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

import test_mcp_interaction as interaction_fixtures
from test_mcp_interaction import (
    connected,
    tool_context,
    CONTEXT,
    sampling_params,
)
from pi_python import *
from pi_python.mcp import *
from pi_python._mcp_host import _SamplingState
from pi_python.providers import (
    OpenAIProvider,
    AnthropicProvider,
    OpenAICompletionsProvider,
    ProviderHTTPError,
    HTTPTransport,
)

endpoint = interaction_fixtures.endpoint

FIXTURES = Path(__file__).parents[1] / "compat/provider-fixtures"


def wire(kind, mode):
    if kind == "completions":
        delta = {"content": '{"ok": true}' if mode == "json" else "你好"}
        if mode in {"tool", "thinking-tool"}:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_x",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"value":"hello"}'},
                    }
                ]
            }
        if mode in {"thinking", "thinking-tool"}:
            delta["reasoning_content"] = "considering"
        chunks = [
            {
                "id": "response-x",
                "model": "test-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "response-x",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls"
                        if mode in {"tool", "thinking-tool"}
                        else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                    "total_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            },
        ]
        return ("".join(f"data: {json.dumps(v)}\n\n" for v in chunks) + "data: [DONE]\n\n").encode()
    source = "tool" if mode == "thinking-tool" else "text" if mode == "json" else mode
    values = json.loads((FIXTURES / f"{kind}-{source}.json").read_text(encoding="utf-8"))["events"]
    if mode == "thinking-tool":
        thinking = json.loads((FIXTURES / f"{kind}-thinking.json").read_text(encoding="utf-8"))[
            "events"
        ]
        if kind == "openai":
            prefix = [v for v in thinking if v.get("output_index") == 0]
            for value in values:
                if "output_index" in value:
                    value["output_index"] = 1
            values[-1]["response"]["output"].insert(0, deepcopy(prefix[-1]["item"]))
            values = prefix + values
        else:
            prefix = [v for v in thinking if v.get("index") in (0, 1)]
            for value in values:
                if "index" in value:
                    value["index"] = 2
            values = [values[0], *prefix, *values[1:]]
    if mode == "json":

        def replace_text(value):
            if isinstance(value, dict):
                return {key: replace_text(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace_text(item) for item in value]
            if value in ("你好", "你"):
                return '{"ok": true}'
            return "" if value == "好" else value

        values = replace_text(values)
    return "".join(f"event: {v['type']}\ndata: {json.dumps(v)}\n\n" for v in values).encode()


@asynccontextmanager
async def local_provider(kind, replies):
    requests, tasks = [], set()
    queue = list(replies)

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            headers = dict(line.split(": ", 1) for line in head.split("\r\n")[1:] if ": " in line)
            body = await reader.readexactly(
                int(next(v for k, v in headers.items() if k.lower() == "content-length"))
            )
            requests.append((json.loads(body), {k.lower(): v for k, v in headers.items()}))
            response = queue.pop(0)
            if callable(response):
                response = response(json.loads(body))
            status, payload, retry = (
                (200, response, None) if isinstance(response, bytes) else response
            )
            extra = f"Retry-After: {retry}\r\n" if retry is not None else ""
            writer.write(
                (
                    f"HTTP/1.1 {status} Result\r\nContent-Type: text/event-stream\r\nContent-Length: {len(payload)}\r\n{extra}Connection: close\r\n\r\n"
                ).encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    api = {
        "openai": "openai-responses",
        "anthropic": "anthropic-messages",
        "completions": "openai-completions",
    }[kind]
    model = ModelInfo(
        "test-model",
        {"anthropic": "anthropic", "completions": "openai-compatible", "openai": "openai"}[kind],
        api,
        "Test",
        100000,
        4096,
        True,
        ("text",),
    )
    cls = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "completions": OpenAICompletionsProvider,
    }[kind]
    provider = cls(
        api_key="host-secret",
        base_url=f"http://127.0.0.1:{port}/v1",
        catalog=ModelCatalog([model]),
        transport=HTTPTransport(max_retries=0),
    )
    try:
        yield provider, model, requests
    finally:
        await provider.aclose()
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("kind", ["openai", "anthropic", "completions"])
@pytest.mark.parametrize("mode", ["tool", "thinking", "thinking-tool"])
async def test_real_provider_roundtrip_and_replay(endpoint, kind, mode):
    observations, parsed = [], []

    async def observe(event):
        observations.append(event)

    async with local_provider(kind, [wire(kind, mode), wire(kind, "text")]) as (
        provider,
        model,
        requests,
    ):
        # Capture the actual parsed message, without substituting a fake Provider.
        class Capture:
            async def stream(self, request, cancel):
                async for event in provider.stream(request, cancel):
                    if event.type == "done":
                        parsed.append(deepcopy(event.message))
                    yield event

        handler = SamplingHandler(
            Capture(),
            model=model,
            options={"reasoning": "medium"},
            tool_choice_format="anthropic" if kind == "anthropic" else "openai",
            observe=observe,
        )
        async with connected(endpoint, MCPCallbacks(sampling=handler)) as tools:
            result = await tools["provider_loop"].execute(
                {"label": "task-a", "followup": mode == "thinking"}, tool_context("task-a")
            )
        assert not result.is_error, result
        assert json.loads(result.content[0].text)["text"] == "你好"
        assert len(requests) == len(observations) == 2
        second = requests[1][0]
        if kind == "openai":
            if mode in {"tool", "thinking-tool"}:
                call = next(i for i in second["input"] if i.get("type") == "function_call")
                reply = next(i for i in second["input"] if i.get("type") == "function_call_output")
                assert call["id"] == "fc_x" and call["call_id"] == reply["call_id"] == "call_x"
            else:
                assert "opaque-encrypted" in json.dumps(second)
                assert any(
                    i.get("id") == "msg_x" and i.get("phase") == "final_answer"
                    for i in second["input"]
                )
                assert parsed[0].content[1].text_signature
            if mode == "thinking-tool":
                assert "opaque-encrypted" in json.dumps(second)
        elif kind == "anthropic":
            assert (
                "opaque-signature" if mode in {"thinking", "thinking-tool"} else "tool_use_id"
            ) in json.dumps(second)
        else:
            assert (
                "reasoning_content" if mode in {"thinking", "thinking-tool"} else "tool_call_id"
            ) in json.dumps(second)
        assert all(
            o.context.run_id == "task-a" and o.context.server == "business" for o in observations
        )
        assert all(o.usage for o in observations)
        assert observations[0].usage == parsed[0].usage
        assert any("cache" in key.lower() for key in observations[0].usage)


@pytest.mark.parametrize("kind", ["openai", "anthropic", "completions"])
async def test_profiles_credentials_hooks_and_auxiliary_tools(endpoint, kind):
    payloads, responses, streams, observed = [], [], [], []

    async def prepare(context, profile, request):
        assert context.run_id == "profiles"
        request.api_key = "request-key"
        request.on_payload = lambda body: payloads.append(deepcopy(body))
        request.on_response = lambda response: responses.append(response)
        request.on_provider_stream_event = lambda event: streams.append(event)

    async def observe(event):
        observed.append(event)

    async with local_provider(
        kind, [wire(kind, "tool"), wire(kind, "text"), wire(kind, "json"), wire(kind, "text")]
    ) as (provider, model, requests):
        fmt = "anthropic" if kind == "anthropic" else "openai"
        options = (
            {"tool_choice": {"type": "auto", "disable_parallel_tool_use": True}}
            if kind == "anthropic"
            else {"sampling_params": {"parallel_tool_calls": False}}
            if kind == "completions"
            else {"parallel_tool_calls": False}
        )
        json_option = (
            {"text": {"format": {"type": "json_object"}}}
            if kind == "openai"
            else {
                "output_config": {"format": {"type": "json_schema", "schema": {"type": "object"}}}
            }
            if kind == "anthropic"
            else {
                "sampling_params": {
                    "response_format": {"type": "json_object"},
                    "parallel_tool_calls": False,
                }
            }
        )
        handler = SamplingHandler(
            provider,
            model=model,
            options=options,
            tool_choice_format=fmt,
            profiles={
                "json": SamplingProfile(model, {**options, **json_option}, fmt),
                "text": SamplingProfile(model, options, fmt),
            },
            prepare=prepare,
            observe=observe,
        )
        async with connected(endpoint, MCPCallbacks(sampling=handler)) as tools:
            result = await tools["provider_loop"].execute(
                {"label": "profiles", "profiles": True}, tool_context("profiles")
            )
        assert not result.is_error, result
        assert json.loads(json.loads(result.content[0].text)["extra"][0]) == {"ok": True}
        assert [o.profile for o in observed] == ["default", "default", "json", "text"]
        assert len(payloads) == len(responses) == 4 and streams
        assert len(requests) == 4
        for body, headers in requests:
            assert headers.get("authorization", headers.get("x-api-key")) in (
                "Bearer request-key",
                "request-key",
            )
        if kind == "anthropic":
            assert requests[0][0]["tool_choice"]["disable_parallel_tool_use"] is True
        else:
            assert requests[0][0]["parallel_tool_calls"] is False
        for body, _ in requests[2:]:
            assert (
                "tools" not in body
                and "tool_choice" not in body
                and "parallel_tool_calls" not in body
            )
        key = "response_format" if kind == "completions" else next(iter(json_option))
        assert key in requests[2][0]
        assert key not in requests[3][0]


async def test_nested_process_provider_and_metering(endpoint):
    observed = []

    async def observe(event):
        observed.append(event)

    async with local_provider("openai", [wire("openai", "tool"), wire("openai", "text")]) as (
        provider,
        model,
        requests,
    ):
        async with connected(
            endpoint, MCPCallbacks(sampling=SamplingHandler(provider, model=model, observe=observe))
        ) as tools:
            result = await tools["nested"].execute(
                {"label": "nested", "child_url": endpoint.get("url", "")}, tool_context("root-task")
            )
        assert not result.is_error, result
        assert len(requests) == len(observed) == 2
        assert all(
            o.context.run_id == "root-task" and o.context.tool_call_id == "root-task"
            for o in observed
        )
        assert len({o.context.request_id for o in observed}) == 2


@pytest.mark.parametrize("status,attempts", [(429, 2), (503, 2), (403, 1), (400, 1)])
async def test_real_provider_retry_is_model_only(endpoint, status, attempts):
    observed = []

    async def observe(event):
        observed.append(event)

    async with local_provider(
        "openai", [(status, b'{"error":"secret-body"}', 0), wire("openai", "text")]
    ) as (provider, model, requests):
        handler = SamplingHandler(
            provider, model=model, retry=SamplingRetryPolicy(2, 0), observe=observe
        )
        async with connected(endpoint, MCPCallbacks(sampling=handler)) as tools:
            result = await tools["provider_loop"].execute({"label": "retry"}, tool_context())
        assert result.is_error == (attempts == 1)
        assert len(requests) == len(observed) == attempts
        assert "secret-body" not in result.content[0].text
        assert observed[0].usage is None
        assert observed[0].category == (
            "rate_limit"
            if status == 429
            else "server"
            if status == 503
            else "authentication"
            if status == 403
            else "request"
        )


def extended_params(conversation="one", profile="default", history=None, **kwargs):
    params = sampling_params(**kwargs)
    params.meta = {
        SAMPLING_EXTENSION: {
            "conversation": conversation,
            "profile": profile,
            "history": history or [],
        }
    }
    return params


async def test_scoped_state_rejects_foreign_modified_or_expired_history():
    signed = AssistantMessage(
        [ThinkingContent("private", "opaque"), TextContent("hello", "signature")],
        provider="real",
        model="host",
        response_id="vendor-id",
    )
    provider = ScriptedProvider([signed, AssistantMessage.text("ok")])
    handler = SamplingHandler(provider, model="host")
    state = _SamplingState()
    context = replace(CONTEXT, _state=state)
    result = await handler(context, extended_params(), CancelToken())
    assert "opaque" not in result.model_dump_json() and "signature" not in result.model_dump_json()
    ref = result.meta[SAMPLING_EXTENSION]["ref"]
    history = [
        {"role": "assistant", "content": {"type": "text", "text": "hello"}},
        {"role": "user", "content": {"type": "text", "text": "again"}},
    ]
    for other in (replace(context, _state=_SamplingState()), context):
        with pytest.raises(UnsupportedCapabilityError):
            await handler(
                other,
                extended_params(conversation="foreign", history=[ref], messages=history),
                CancelToken(),
            )
    changed = deepcopy(history)
    changed[0]["content"]["text"] = "changed"
    with pytest.raises(UnsupportedCapabilityError, match="changed"):
        await handler(context, extended_params(history=[ref], messages=changed), CancelToken())
    await handler(context, extended_params(history=[ref], messages=history), CancelToken())
    assert provider.requests[1].messages[0] == signed
    state.clear()
    with pytest.raises(UnsupportedCapabilityError, match="Expired"):
        await handler(context, extended_params(history=[ref], messages=history), CancelToken())


async def test_retry_after_cancellation_and_no_retry_for_permanent_error():
    started = asyncio.Event()
    observed = []

    class RateLimited:
        def __init__(self):
            self.calls = 0

        async def stream(self, request, cancel):
            self.calls += 1
            raise ProviderHTTPError(429, body="secret", retry_after=120)
            yield

    async def observe(event):
        observed.append(event)
        started.set()

    provider, cancel = RateLimited(), CancelToken()
    handler = SamplingHandler(
        provider, model="host", retry=SamplingRetryPolicy(3, 0), observe=observe
    )
    task = asyncio.create_task(handler(CONTEXT, sampling_params(), cancel))
    await started.wait()
    await asyncio.sleep(0.02)
    assert not task.done() and provider.calls == 1
    cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert observed[0].retry_after == 120 and provider.calls == 1


async def test_nested_cancellation_cleans_callback_and_child_connection(endpoint, tmp_path):
    started, stopped = asyncio.Event(), asyncio.Event()

    class Waiting:
        async def stream(self, request, cancel):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            yield

    marker = tmp_path / "nested-cleaned"
    async with connected(
        endpoint, MCPCallbacks(sampling=SamplingHandler(Waiting(), model="host"))
    ) as tools:
        context = tool_context("cancel-nested")
        task = asyncio.create_task(
            tools["nested"].execute(
                {"label": "wait", "child_url": endpoint.get("url", ""), "marker": str(marker)},
                context,
            )
        )
        await asyncio.wait_for(started.wait(), 10)
        context.cancel.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(stopped.wait(), 5)
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert marker.exists()
        result = await tools["capabilities"].execute({}, tool_context("reused"))
        assert not result.is_error


@pytest.mark.parametrize("kind", ["openai", "anthropic", "completions"])
async def test_missing_provider_usage_stays_unknown(endpoint, kind):
    # Remove usage from the wire, not from a hand-created AssistantMessage.
    raw = wire(kind, "text").decode()
    lines = []
    for line in raw.splitlines():
        if line.startswith("data: {"):
            value = json.loads(line[6:])
            value.pop("usage", None)
            for key in ("message", "response"):
                if key in value:
                    value[key].pop("usage", None)
            line = "data: " + json.dumps(value)
        lines.append(line)
    observed = []

    async def observe(event):
        observed.append(event)

    async with local_provider(kind, [("\n".join(lines) + "\n\n").encode()]) as (
        provider,
        model,
        requests,
    ):
        async with connected(
            endpoint, MCPCallbacks(sampling=SamplingHandler(provider, model=model, observe=observe))
        ) as tools:
            result = await tools["sample"].execute({"label": "unknown-usage"}, tool_context())
        assert not result.is_error and len(requests) == 1
        assert len(observed) == 1 and observed[0].usage is None


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({}, None),
        ({"prompt_tokens": 12}, None),
        ({"prompt_tokens": 0, "completion_tokens": 0}, 0),
        ({"prompt_tokens": 12, "completion_tokens": 8}, 20),
    ],
)
async def test_sampling_metering_distinguishes_unknown_and_zero_usage(usage, expected):
    lines = []
    for line in wire("completions", "text").decode().splitlines():
        if line.startswith("data: {"):
            value = json.loads(line[6:])
            if "usage" in value:
                value["usage"] = usage
            line = "data: " + json.dumps(value)
        lines.append(line)
    observed = []

    async def observe(event):
        observed.append(event)

    async with local_provider("completions", [("\n".join(lines) + "\n\n").encode()]) as (
        provider,
        model,
        requests,
    ):
        await SamplingHandler(provider, model=model, observe=observe)(
            CONTEXT, sampling_params(), CancelToken()
        )
    assert len(requests) == len(observed) == 1
    assert (
        observed[0].usage["total_tokens"] if observed[0].usage is not None else None
    ) == expected


async def test_real_concurrent_service_state_and_task_attribution(endpoint):
    def reply(body):
        label = next(
            item["content"][0]["text"] for item in body["input"] if item.get("role") == "user"
        )
        continued = any(item.get("type") == "reasoning" for item in body["input"])
        data = wire("openai", "text" if continued else "thinking")
        return (
            data.replace(b"opaque-encrypted", f"opaque-{label}".encode())
            .replace("你好".encode(), label.encode())
            .replace(b"\\u4f60", label.encode())
            .replace(b"\\u597d", b"")
        )

    observed = []

    async def observe(event):
        observed.append(event)

    async with local_provider("openai", [reply] * 4) as (provider, model, requests):
        handler = SamplingHandler(
            provider,
            model=model,
            options={"reasoning": "medium"},
            max_concurrency=2,
            observe=observe,
        )
        async with connected(endpoint, MCPCallbacks(sampling=handler)) as one:
            async with connected(endpoint, MCPCallbacks(sampling=handler)) as two:
                results = await asyncio.gather(
                    *[
                        tools["provider_loop"].execute(
                            {"label": label, "followup": True}, tool_context(label)
                        )
                        for tools, label in [(one, "one"), (two, "two")]
                    ]
                )
        for result, label in zip(results, ("one", "two")):
            assert not result.is_error, result
            assert json.loads(result.content[0].text)["text"] == label
        for body, _ in requests:
            label = next(i["content"][0]["text"] for i in body["input"] if i.get("role") == "user")
            signatures = [
                i["encrypted_content"] for i in body["input"] if i.get("type") == "reasoning"
            ]
            assert signatures in ([], [f"opaque-{label}"])
        assert sorted(o.context.run_id for o in observed) == ["one", "one", "two", "two"]
        assert all(not o.context._state.responses for o in observed)


async def test_profile_denial_and_capability_requirement_before_execution():
    from types import SimpleNamespace
    from mcp import types

    provider = ScriptedProvider([])
    handler = SamplingHandler(provider, model="host")
    with pytest.raises(PermissionError, match="profile"):
        await handler(
            replace(CONTEXT, _state=_SamplingState()),
            extended_params(profile="secret-model"),
            CancelToken(),
        )
    with pytest.raises(UnsupportedCapabilityError, match="host state"):
        async with SamplingProvider(
            SimpleNamespace(
                protocol_version="2025-11-25",
                request_id=1,
                session=SimpleNamespace(
                    client_capabilities=types.ClientCapabilities(
                        sampling=types.SamplingCapability()
                    )
                ),
            ),
            require_host_state=True,
        ):
            pass
    assert not provider.requests


async def test_retry_observer_once_for_each_response_and_unknown_usage():
    observations = []

    async def observe(event):
        observations.append(event)

    failed = AssistantMessage(
        [],
        stop_reason="error",
        error="secret 429",
        usage={"input": 1, "cache_write": 3},
        diagnostics=[{"type": "provider_http_error", "status": 429}],
    )
    success = AssistantMessage.text("ok", usage={"input": 2, "cache_read": 5})
    provider = ScriptedProvider([[ModelEvent("error", message=failed, reason="error")], success])
    result = await SamplingHandler(
        provider, model="host", retry=SamplingRetryPolicy(2, 0), observe=observe
    )(CONTEXT, sampling_params(), CancelToken())
    assert result.content.text == "ok"
    assert [o.attempt for o in observations] == [1, 2]
    assert [o.usage for o in observations] == [failed.usage, success.usage]
    assert "secret" not in repr(observations)


async def test_signed_tool_call_state_is_restored():
    original = AssistantMessage(
        [ToolCall("call", "echo", {"value": "hi"}, "vendor-tool-signature")],
        "tool_use",
        provider="real",
        model="host",
    )
    provider = ScriptedProvider([original, AssistantMessage.text("done")])
    handler = SamplingHandler(provider, model="host")
    context = replace(CONTEXT, _state=_SamplingState())
    tools = [{"name": "echo", "inputSchema": {"type": "object"}}]
    first = await handler(context, extended_params(tools=tools), CancelToken())
    ref = first.meta[SAMPLING_EXTENSION]["ref"]
    history = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call", "name": "echo", "input": {"value": "hi"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "toolUseId": "call",
                    "content": [{"type": "text", "text": "hi"}],
                }
            ],
        },
    ]
    await handler(
        context, extended_params(history=[ref], messages=history, tools=tools), CancelToken()
    )
    assert provider.requests[1].messages[0] == original


@pytest.mark.parametrize(
    "status,attempts,category",
    [(429, 2, "rate_limit"), (503, 2, "server"), (403, 1, "authentication")],
)
async def test_safe_error_contract_reaches_server(endpoint, status, attempts, category):
    async with local_provider(
        "openai", [(status, b'{"error":"sensitive-body"}', 0)] * attempts
    ) as (provider, model, requests):
        async with connected(
            endpoint,
            MCPCallbacks(
                sampling=SamplingHandler(provider, model=model, retry=SamplingRetryPolicy(2, 0))
            ),
        ) as tools:
            result = await tools["sampling_error"].execute({}, tool_context())
        data = json.loads(result.content[0].text)
        assert data["code"] == -32002
        assert data["data"] == {
            "category": category,
            "retryable": status != 403,
            "retryAfter": 0,
            "attempts": attempts,
            "retryOwner": "host",
        }
        assert len(requests) == attempts
        assert "sensitive" not in result.content[0].text


async def test_model_retry_does_not_reexecute_completed_tool(endpoint):
    async with local_provider(
        "openai", [wire("openai", "tool"), (429, b"{}", 0), wire("openai", "text")]
    ) as (provider, model, requests):
        async with connected(
            endpoint,
            MCPCallbacks(
                sampling=SamplingHandler(provider, model=model, retry=SamplingRetryPolicy(2, 0))
            ),
        ) as tools:
            result = await tools["provider_loop"].execute({"label": "once"}, tool_context())
        assert not result.is_error
        assert json.loads(result.content[0].text)["tool_results"] == 1
        assert len(requests) == 3
        assert requests[1][0] == requests[2][0]


async def test_observer_failure_never_retries_completed_response():
    provider = ScriptedProvider([AssistantMessage.text("generated", usage={"output": 1})])

    async def broken(event):
        raise RuntimeError("meter unavailable")

    with pytest.raises(RuntimeError, match="meter unavailable"):
        await SamplingHandler(
            provider, model="host", observe=broken, retry=SamplingRetryPolicy(3, 0)
        )(CONTEXT, sampling_params(), CancelToken())
    assert len(provider.requests) == 1


async def test_unsigned_protocol_rejects_reasoning_before_model_access():
    provider = ScriptedProvider([])
    with pytest.raises(UnsupportedCapabilityError, match="requires.*host-state"):
        await SamplingHandler(provider, model="host", options={"reasoning": "medium"})(
            CONTEXT, sampling_params(), CancelToken()
        )
    assert not provider.requests


async def test_cancellation_releases_retained_vendor_state(endpoint):
    contexts = []
    waiting = asyncio.Event()
    closed = asyncio.Event()

    class Provider:
        def __init__(self):
            self.calls = 0

        async def stream(self, request, cancel):
            self.calls += 1
            if self.calls == 1:
                yield ModelEvent.done(
                    AssistantMessage([TextContent("first", "retained-signature")])
                )
                return
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            yield

    async def prepare(context, profile, request):
        contexts.append(context)

    async with connected(
        endpoint, MCPCallbacks(sampling=SamplingHandler(Provider(), model="host", prepare=prepare))
    ) as tools:
        context = tool_context("retained-cancel")
        task = asyncio.create_task(
            tools["provider_loop"].execute({"label": "cancel", "followup": True}, context)
        )
        await asyncio.wait_for(waiting.wait(), 5)
        assert contexts[0]._state.responses
        context.cancel.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(closed.wait(), 5)
        assert all(not context._state.responses for context in contexts)


async def test_aborted_provider_terminal_is_not_retried():
    observed = []

    async def observe(event):
        observed.append(event)

    aborted = AssistantMessage([], stop_reason="aborted", usage={"output": 2})
    provider = ScriptedProvider([[ModelEvent("error", message=aborted, reason="aborted")]])
    with pytest.raises(asyncio.CancelledError):
        await SamplingHandler(
            provider, model="host", retry=SamplingRetryPolicy(3, 0), observe=observe
        )(CONTEXT, sampling_params(), CancelToken())
    assert len(provider.requests) == len(observed) == 1
    assert observed[0].status == "cancelled" and observed[0].usage == {"output": 2}


@pytest.mark.parametrize("mode", ["auto", "websocket", "websocket-cached"])
async def test_unsupported_provider_transport_fails_before_generation(mode):
    provider = ScriptedProvider([])
    with pytest.raises(ConfigurationError, match="SSE"):
        SamplingHandler(provider, model="host", options={"transport": mode})

    async def prepare(context, profile, request):
        request.options["transport"] = mode

    with pytest.raises(UnsupportedCapabilityError, match="SSE"):
        await SamplingHandler(provider, model="host", prepare=prepare)(
            CONTEXT, sampling_params(), CancelToken()
        )
    assert not provider.requests


async def test_host_bound_hooks_keep_identity_across_retry():
    class Recorder:
        def __init__(self):
            self.seen = []

        def record(self, value):
            self.seen.append(value)

    recorder = Recorder()

    class Provider:
        def __init__(self):
            self.calls = 0

        async def stream(self, request, cancel):
            self.calls += 1
            assert request.api_key == "host-only"
            for hook in (request.on_payload, request.on_response, request.on_provider_stream_event):
                assert hook.__self__ is recorder
                hook(self.calls)
            request.messages.clear()
            if self.calls == 1:
                raise ProviderHTTPError(503)
            yield ModelEvent.done(AssistantMessage.text("ok"))

    async def prepare(context, profile, request):
        request.api_key = "host-only"
        request.on_payload = request.on_response = request.on_provider_stream_event = (
            recorder.record
        )

    provider = Provider()
    await SamplingHandler(provider, model="host", prepare=prepare, retry=SamplingRetryPolicy(2, 0))(
        CONTEXT, sampling_params(), CancelToken()
    )
    assert recorder.seen == [1, 1, 1, 2, 2, 2]


async def test_synchronous_stream_creation_error_is_classified():
    observed = []

    class Provider:
        def stream(self, request, cancel):
            raise ProviderHTTPError(403, body="secret")

    async def observe(event):
        observed.append(event)

    with pytest.raises(SamplingFailure) as caught:
        await SamplingHandler(
            Provider(), model="host", observe=observe, retry=SamplingRetryPolicy(3, 0)
        )(CONTEXT, sampling_params(), CancelToken())
    assert caught.value.category == "authentication" and caught.value.attempts == 1
    assert len(observed) == 1 and observed[0].usage is None
    assert "secret" not in str(caught.value)
