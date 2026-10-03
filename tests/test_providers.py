import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from pi_python import *
from pi_python.providers import (
    AnthropicProvider,
    OpenAIProvider,
    OpenAICodexProvider,
    HTTPTransport,
    ProviderHTTPError,
    OAuthCredential,
)
from pi_python.providers.transport import sse_events
from pi_python.proxy import pi_message

FIXTURES = Path(__file__).resolve().parents[1] / "compat/provider-fixtures"
# The record the upstream differential runner uses for its synthetic `test-model`.
TEST_CATALOG = ModelCatalog(
    [
        *ModelCatalog.bundled().list(),
        *(
            ModelInfo("test-model", name, api, "Test", 100000, 4096, True, ("text", "image"))
            for name, api in [
                ("anthropic", "anthropic-messages"),
                ("openai", "openai-responses"),
                ("openai-codex", "openai-codex-responses"),
            ]
        ),
    ]
)


def events(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["events"]


def sse(values):
    return "".join(
        "event: " + v["type"] + "\ndata: " + json.dumps(v, ensure_ascii=False) + "\n\n"
        for v in values
    ).encode()


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False

    async def __aiter__(self):
        for start in range(0, len(self.data), 7):
            yield self.data[start : start + 7]

    async def aclose(self):
        self.closed = True


def remote(kind, sequences, **kwargs):
    requests = []
    streams = []

    async def handler(request):
        requests.append(request)
        stream = ByteStream(sse(sequences.pop(0)))
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HTTPTransport(client)
    provider = (
        AnthropicProvider(api_key="test-key", transport=transport, catalog=TEST_CATALOG, **kwargs)
        if kind == "anthropic"
        else OpenAIProvider(api_key="test-key", transport=transport, catalog=TEST_CATALOG, **kwargs)
    )
    return provider, requests, streams, client


@pytest.mark.parametrize("kind", ["anthropic", "openai"])
async def test_real_wire_tool_loop(kind):
    provider, requests, streams, client = remote(
        kind, [events(kind + "-tool"), events(kind + "-text")]
    )
    effects = []

    async def execute(args, ctx):
        effects.append(args)
        return ToolResult([TextContent("hello"), ImageContent("aGk=", "image/png")])

    tool = Tool(
        "echo",
        "Echo",
        {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        execute,
    )
    async with client:
        agent = Agent(provider=provider, model="test-model", tools=[tool])
        result = await agent.prompt("测试", [ImageContent("aGk=", "image/png")])
    assert result.status == "completed", result.errors
    assert effects == [{"value": "hello"}]
    assert len(requests) == 2 and all(s.closed for s in streams)
    body = json.loads(requests[1].content)
    assert (
        "data:image/png;base64,aGk=" in json.dumps(body)
        if kind == "openai"
        else "media_type" in json.dumps(body)
    )
    assert result.messages[-1].content[0].text == "你好"


@pytest.mark.parametrize("kind", ["anthropic", "openai"])
async def test_thinking_signatures_replayed(kind):
    provider, requests, streams, client = remote(
        kind, [events(kind + "-thinking"), events(kind + "-text")]
    )
    async with client:
        agent = Agent(
            provider=provider,
            model="test-model",
            thinking_level="medium",
            thinking_budgets={"medium": 2048},
        )
        result = await agent.prompt("reason")
        assert result.status == "completed", result.errors
        signed = result.messages[-1].content[0]
        assert isinstance(signed, ThinkingContent) and signed.thinking_signature
        assert decode_messages(encode_messages(list(agent.state.messages))) == list(
            agent.state.messages
        )
        result = await agent.prompt("continue")
    assert result.status == "completed", result.errors
    body = json.loads(requests[1].content)
    assert ("opaque-signature" if kind == "anthropic" else "opaque-encrypted") in json.dumps(body)
    assert ("thinking" if kind == "anthropic" else "reasoning") in body


@pytest.mark.parametrize("kind", ["anthropic", "openai"])
@pytest.mark.parametrize("corruption", ["truncated", "bad_json", "bad_final", "error"])
async def test_broken_stream_never_executes_tool(kind, corruption):
    values = events(kind + "-tool")
    if corruption == "truncated":
        values = values[:-1]
    elif corruption == "bad_json":
        if kind == "anthropic":
            values[2]["delta"]["partial_json"] = "invalid"
        else:
            values[1]["delta"] = "invalid"
    elif corruption == "bad_final":
        if kind == "anthropic":
            values[-2]["delta"]["stop_reason"] = "refusal"
        else:
            values[-1]["response"]["output"][0]["arguments"] = '{"value":"different"}'
    else:
        values.insert(-1, {"type": "error", "error": {"message": "failed"}})
    provider, _, streams, client = remote(kind, [values])
    effects = []

    async def execute(args, ctx):
        effects.append(args)
        return ToolResult([TextContent("bad")])

    async with client:
        result = await Agent(
            provider=provider,
            model="test-model",
            tools=[Tool("echo", "", {"type": "object"}, execute)],
        ).prompt("go")
    assert result.status == "failed"
    assert effects == [] and all(s.closed for s in streams)


async def test_auth_payload_response_and_raw_event_hooks():
    provider, requests, _, client = remote("openai", [events("openai-text")])
    seen = []

    async def key(provider_name):
        seen.append(provider_name)
        return "callback-key"

    async def payload(body):
        body["metadata"] = {"test": "yes"}

    async with client:
        result = await Agent(
            provider=provider,
            model="test-model",
            get_api_key=key,
            on_payload=payload,
            on_response=lambda value: seen.append(value["status"]),
            on_provider_stream_event=lambda value: seen.append(value["type"]),
        ).prompt("go")
    assert result.status == "completed"
    assert seen[0] == "openai" and 200 in seen and "response.completed" in seen
    assert requests[0].headers["authorization"] == "Bearer callback-key"
    assert json.loads(requests[0].content)["metadata"] == {"test": "yes"}


async def test_claude_subscription_headers_and_tool_case():
    values = events("anthropic-tool")
    values[1]["content_block"]["name"] = "Read"
    provider, requests, _, client = remote("anthropic", [values], auth_mode="oauth")
    provider.api_key = "sk-ant-oat-test"
    request = ModelRequest(
        [SystemMessage("ours"), UserMessage("go")],
        [ToolDeclaration("read", "", {"type": "object"})],
        model="test-model",
    )
    async with client:
        result = [e async for e in provider.stream(request, CancelToken())][-1].message
    body = json.loads(requests[0].content)
    assert requests[0].headers["authorization"] == "Bearer sk-ant-oat-test"
    assert "x-api-key" not in requests[0].headers
    assert "oauth-2025-04-20" in requests[0].headers["anthropic-beta"]
    assert body["tools"][0]["name"] == "Read" and body["system"][1]["text"] == "ours"
    assert result.tool_calls[0].name == "read"


def codex_token():
    value = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"https://api.openai.com/auth": {"chatgpt_account_id": "account-fixture"}}
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    return "header." + value + ".signature"


@pytest.mark.parametrize("kind", ["direct", "codex"])
async def test_openai_subscription_uses_correct_endpoint(kind):
    async def handler(request):
        captured.append(request)
        return httpx.Response(200, content=sse(events("openai-text")))

    captured = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credential = OAuthCredential(
            "openai-chatgpt" if kind == "direct" else "openai-codex",
            codex_token(),
            "refresh",
            10**12,
            scopes=("chatgpt.tokens.use.direct",),
        )
        provider = (OpenAIProvider if kind == "direct" else OpenAICodexProvider)(
            credentials=credential, transport=HTTPTransport(client), catalog=TEST_CATALOG
        )
        result = await Agent(provider=provider, model="test-model", session_id="session").prompt(
            "go"
        )
    assert result.status == "completed", result.errors
    assert str(captured[0].url) == (
        "https://api.openai.com/v1/responses"
        if kind == "direct"
        else "https://chatgpt.com/backend-api/codex/responses"
    )
    if kind == "codex":
        assert captured[0].headers["chatgpt-account-id"] == "account-fixture"
        assert json.loads(captured[0].content)["instructions"] == "You are a helpful assistant."


async def test_sse_framing_unicode_comments_crlf_and_multiline():
    data = (
        ': ping\r\nevent: text\r\ndata: {"text":\r\ndata: "你好"}\r\n\r\ndata: [DONE]\n\n'.encode()
    )

    async def chunks():
        for b in data:
            yield bytes([b])

    assert [v async for v in sse_events(chunks())] == [
        {"type": "text", "text": "你好"},
        {"type": "transport_done"},
    ]


@pytest.mark.parametrize("data", [b"data: []\n\n", b"data: invalid\n\n"])
async def test_sse_rejects_invalid_data(data):
    async def chunks():
        yield data

    with pytest.raises(ProviderProtocolError):
        [v async for v in sse_events(chunks())]


async def test_http_error_closes_response_and_redacts_credentials_from_body():
    # As in Pi, the server's error body is kept (capped) so causes such as context
    # overflow are visible; the credential sent with the request is redacted.
    stream = ByteStream(
        b'{"error": "bad key sk-live-0123456789", "detail": "' + b"x" * 5000 + b'"}'
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(401, stream=stream))
    ) as client:
        with pytest.raises(ProviderHTTPError, match="401") as error:
            [
                v
                async for v in HTTPTransport(client).stream(
                    "https://example.test",
                    {},
                    {"authorization": "Bearer sk-live-0123456789"},
                    CancelToken(),
                )
            ]
    message = str(error.value)
    assert stream.closed and "bad key [redacted]" in message and "sk-live" not in message
    assert error.value.body[:4000].endswith("x") and "... [truncated " in error.value.body[4000:]


async def test_local_http_socket_and_abort():
    seen = []
    closed = asyncio.Event()

    async def handle(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        size = int(
            next(
                line.split(b":")[1]
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
        )
        seen.append(json.loads(await reader.readexactly(size)))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
            + sse(events("openai-text")[:2])
        )
        await writer.drain()
        await reader.read()
        writer.close()
        await writer.wait_closed()
        closed.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    agent = Agent(
        provider=OpenAIProvider(
            api_key="fixture", base_url=f"http://127.0.0.1:{port}", catalog=TEST_CATALOG
        ),
        model="test-model",
    )
    agent.subscribe(lambda event: agent.abort() if event.type == "message_update" else None)
    async with server:
        result = await asyncio.wait_for(agent.prompt("hello"), 3)
        await asyncio.wait_for(closed.wait(), 3)
    assert result.status == "cancelled" and seen[0]["stream"] is True


async def test_local_websocket_inference():
    from websockets.asyncio.server import serve

    requests = []

    async def handle(socket):
        requests.append(json.loads(await socket.recv()))
        for event in events("openai-thinking"):
            await socket.send(json.dumps(event))

    seen = []
    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        agent = Agent(
            provider=OpenAIProvider(
                api_key="fixture", base_url=f"http://127.0.0.1:{port}", catalog=TEST_CATALOG
            ),
            model="test-model",
            transport="websocket",
            on_response=lambda value: seen.append(value["status"]),
        )
        result = await agent.prompt("hello")
    assert result.status == "completed", result.errors
    assert requests[0]["type"] == "response.create" and "stream" not in requests[0]
    assert seen == [101]


@pytest.mark.parametrize("fixture", ["proxy-text", "proxy-thinking-tool"])
async def test_proxy_wire_and_whitelisted_options(fixture):
    captured = []

    async def handler(request):
        captured.append(request)
        return httpx.Response(200, content=sse(events(fixture)))

    model = {"id": "test-model", "provider": "openai", "api": "openai-responses"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = [
            e
            async for e in stream_proxy(
                model,
                [UserMessage("go")],
                {
                    "proxy_url": "https://proxy.test",
                    "auth_token": "private",
                    "max_tokens": 128,
                    "session_id": "abc",
                    "unrelated": "secret",
                },
                transport=HTTPTransport(client),
            )
        ]
    body = json.loads(captured[0].content)
    assert str(captured[0].url) == "https://proxy.test/api/stream"
    assert body["options"] == {"maxTokens": 128, "sessionId": "abc"}
    assert body["model"] == model and result[-1].type == "done"


def test_proxy_preserves_schema_arguments_and_tool_removals():
    call = AssistantMessage(
        [
            ToolCall(
                "x", "t", {"type": "tool_call", "call_id": "key", "data": {"mime_type": "literal"}}
            )
        ]
    )
    assert pi_message(call)["content"][0]["arguments"] == call.tool_calls[0].arguments
    system = SystemMessage(
        tools_added=[ToolDeclaration("t", "", {"properties": {"call_id": {"type": "string"}}})],
        tools_removed=["x"],
    )
    assert pi_message(system)["toolsAdded"][0]["parameters"] == system.tools_added[0].input_schema
    assert pi_message(system)["toolsRemoved"] == [{"name": "x"}]


@pytest.mark.parametrize("kind", ["anthropic", "openai"])
async def test_missing_auth_fails_without_network(kind):
    provider = (AnthropicProvider if kind == "anthropic" else OpenAIProvider)(catalog=TEST_CATALOG)
    result = await Agent(provider=provider, model="test-model").prompt("go")
    assert result.status == "failed" and "Missing credentials" in result.errors[0]


@pytest.mark.parametrize("level", ["minimal", "low", "medium", "high", "xhigh", "max"])
def test_claude_thinking_budget_respects_model_ceiling(level):
    model = ModelInfo("budget", "anthropic", "anthropic-messages", "Budget", 100000, 10000, True)
    body = AnthropicProvider().build_request(
        ModelRequest(
            [UserMessage("go")],
            model="budget",
            options={"reasoning": level, "max_tokens": 2048},
            model_info=model,
        )
    )
    assert 2048 <= body["max_tokens"] <= 10000
    assert 1024 <= body["thinking"]["budget_tokens"] <= body["max_tokens"] - 1024


def test_cross_provider_tool_id_mapping_and_signature_drop():
    assistant = AssistantMessage(
        [ThinkingContent("reason", "opaque-foreign"), ToolCall("call_x|fc_x", "echo", {})],
        provider="openai",
        api="openai-responses",
        model="gpt",
    )
    result = ToolResultMessage("call_x|fc_x", "echo", [TextContent("ok")])
    body = AnthropicProvider(catalog=TEST_CATALOG).build_request(
        ModelRequest([assistant, result], model="test-model")
    )
    assert body["messages"][0]["content"][1]["id"] == "call_x_fc_x"
    assert body["messages"][1]["content"][0]["tool_use_id"] == "call_x_fc_x"
    assert "opaque-foreign" not in json.dumps(body)


@pytest.mark.parametrize("kind", ["anthropic", "openai"])
async def test_usage_normalizes_cached_tokens(kind):
    provider, _, _, client = remote(kind, [events(kind + "-text")])
    async with client:
        result = await Agent(provider=provider, model="test-model").prompt("go")
    assert result.usage["input"] == (12 if kind == "anthropic" else 10)
    assert result.usage["output"] == 8 and result.usage["total_tokens"] == 20
    assert "cost" not in result.usage


@pytest.mark.parametrize("fault", ["truncated", "missing_end", "late_delta"])
async def test_proxy_invalid_terminal_protocol(fault):
    values = events("proxy-thinking-tool")
    if fault == "truncated":
        values = values[:-1]
    elif fault == "missing_end":
        values.pop(-2)
    else:
        values.insert(-1, {"type": "toolcall_delta", "contentIndex": 1, "delta": " "})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=sse(values)))
    ) as client:
        provider = ProxyProvider(
            model={"id": "test", "api": "test", "provider": "test"},
            proxy_url="https://proxy.test",
            auth_token="fixture",
            transport=HTTPTransport(client),
        )
        effects = []

        async def execute(args, ctx):
            effects.append(args)
            return ToolResult.text("executed")

        agent = Agent(provider=provider, tools=[Tool("echo", "", {"type": "object"}, execute)])
        result = await agent.prompt("go")
    assert result.status == "failed" and effects == []
