import asyncio
from copy import deepcopy
import pytest
from pi_python import *
from pi_python.providers import AnthropicProvider
from pi_python.stream import event_contract, partial_json


def test_complete_metadata_roundtrip_and_user_null_keys_preserved():
    metadata = {
        "usage": None,
        "error": None,
        "text_signature": None,
        "details": {"type": "tool_call"},
    }
    messages = [
        SystemMessage([TextContent("one"), TextContent("two")]),
        AssistantMessage(
            [TextContent("answer"), ToolCall("id", "tool", {})],
            stop_reason="tool_use",
            response_id="r",
            response_model="native",
            thinking_level="max",
            provider_thinking_level="high",
            raw_stop_reason="end_turn",
            end_turn=True,
            diagnostics=[metadata],
        ),
        ToolResultMessage(
            "id",
            "tool",
            [TextContent("value")],
            details=metadata,
            usage={"input": 1},
            nested_calls={
                "complete": True,
                "calls": [{"id": "child", "name": "nested", "status": "ok"}],
            },
        ),
    ]
    assert decode_messages(encode_messages(messages)) == messages
    # Pi contentText joins the text blocks of one system message with a single newline.
    assert current_system_prompt(messages) == "one\ntwo"
    assert message_to_dict(messages[1])["diagnostics"][0] == metadata
    from pi_python.proxy import pi_message

    assert pi_message(messages[-1])["details"] == metadata


def test_pending_not_committed_and_deferred_roundtrip():
    with pytest.raises(MessageValidationError):
        validate_history([AssistantMessage([], stop_reason="pending")])
    message = AssistantMessage(
        [],
        stop_reason="deferred",
        deferred={"provider": "p", "model_id": "m", "api": "a", "id": "job", "poll_after_ms": 100},
    )
    assert decode_messages(encode_messages([message])) == [message]


class FullProvider:
    name = "test"

    def __init__(self, invalid=False):
        self.invalid = invalid

    @event_contract
    async def stream(self, request, cancel):
        yield ModelEvent("start")
        yield ModelEvent.boundary("start", 0, TextContent(""))
        yield ModelEvent.text("hello")
        yield ModelEvent.boundary("start", 1, ToolCall("call", "add", {}))
        yield ModelEvent.toolcall('{"a":', 1)
        yield ModelEvent.toolcall("2}", 1)
        yield ModelEvent.boundary("end", 0, TextContent("hello"))
        yield ModelEvent.boundary("end", 1, ToolCall("call", "add", {"a": 2}))
        yield ModelEvent.done(
            AssistantMessage(
                [
                    TextContent("changed" if self.invalid else "hello"),
                    ToolCall("call", "add", {"a": 2}),
                ],
                "tool_use",
            )
        )


async def test_full_events_interleaving_partial_snapshots_and_strict_terminal():
    events = [e async for e in FullProvider().stream(ModelRequest([]), CancelToken())]
    assert [e.type for e in events] == [
        "start",
        "text_start",
        "text_delta",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_delta",
        "text_end",
        "toolcall_end",
        "done",
    ]
    assert events[0].partial.content == []
    assert events[1].partial.content[0].text == ""
    assert events[5].partial.tool_calls[0].arguments == {"a": 2}
    events[5].partial.tool_calls[0].arguments["a"] = 999
    assert events[-1].message.tool_calls[0].arguments == {"a": 2}
    events = [e async for e in FullProvider(True).stream(ModelRequest([]), CancelToken())]
    assert events[-1].type == "error" and events[-1].reason == "error"
    assert "mismatch" in events[-1].message.error


async def test_invalid_stream_never_executes_tool():
    effects = []

    async def execute(args, context):
        effects.append(args)
        return ToolResult.text("done")

    result = await Agent(
        provider=FullProvider(True), tools=[Tool("add", "", {"type": "object"}, execute)]
    ).prompt("go")
    assert result.status == "failed" and effects == []


async def test_hook_context_and_tool_metadata_persist_but_do_not_reach_model():
    captured = []

    async def execute(args, context):
        assert context.assistant_message.tool_calls[0].id == "call"
        assert context.agent_context.message.tool_calls[0].name == "add"
        context.assistant_message.content.clear()
        return ToolResult.text(
            "2",
            details={"secret": "private"},
            usage={"input": 1},
            nested_calls={"complete": True, "calls": []},
        )

    def after(call, result, context):
        assert context.args == {"a": 2} and context.result.details == {"secret": "private"}
        assert context.is_error is False
        return ToolResultUpdate(usage={"input": 3})

    def finish(context, cancel):
        captured.append(deepcopy(context))

    provider = ScriptedProvider(
        [
            AssistantMessage([ToolCall("call", "add", {"a": 2})], "tool_use"),
            AssistantMessage.text("done"),
        ]
    )
    agent = Agent(
        provider=provider,
        tools=[Tool("add", "", {"type": "object"}, execute)],
        hooks=Hooks(after_tool_call=after, finish_turn=finish),
    )
    result = await agent.prompt("go")
    assert result.status == "completed", result.errors
    stored = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    assert stored.details == {"secret": "private"} and stored.usage == {"input": 3}
    sent = next(m for m in provider.requests[-1].messages if isinstance(m, ToolResultMessage))
    assert sent.details is sent.usage is sent.nested_calls is None
    assert captured[-1].new_messages[-1].content[0].text == "done"


def test_signature_requires_same_api_and_native_effort_is_mapped():
    message = AssistantMessage(
        [ThinkingContent("reason", "opaque")],
        provider="anthropic",
        model="claude-sonnet-4-6",
        api="different-api",
    )
    request = ModelRequest(
        [message, UserMessage("go")], model="claude-sonnet-4-6", options={"reasoning": "minimal"}
    )
    body = AnthropicProvider(api_key="fixture").build_request(request)
    assert body["messages"][0]["content"][0] == {"type": "text", "text": "reason"}
    # Pi requests summarized thinking; Opus 4.7+ would otherwise omit thinking text.
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"]["effort"] == "low"


@pytest.mark.parametrize(
    "value,expected", [('{"a":2', {"a": 2}), ('{"a":"part', {"a": "part"}), ('{"a":', {})]
)
def test_partial_arguments_are_preview_only(value, expected):
    assert partial_json(value) == expected


async def test_custom_canonical_provider_missing_end_cannot_execute():
    async def stream(request, cancel):
        yield ModelEvent("start", partial=AssistantMessage([], stop_reason="pending"))
        yield ModelEvent.boundary("start", 0, ToolCall("call", "add", {}))
        yield ModelEvent(
            "done", message=AssistantMessage([ToolCall("call", "add", {})], "tool_use")
        )

    result = await Agent(stream_fn=stream).prompt("go")
    assert result.status == "failed" and "Terminal with unfinished blocks" in result.errors[0]


async def test_http_failure_exposes_structured_diagnostic_without_false_start():
    import httpx
    from pi_python.providers import HTTPTransport

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(401, content="invalid x-api-key sk-ant-fixture-key")
        )
    ) as client:
        provider = AnthropicProvider(api_key="sk-ant-fixture-key", transport=HTTPTransport(client))
        request = ModelRequest([UserMessage("go")], model="claude-sonnet-4-5")
        events = [e async for e in provider.stream(request, CancelToken())]
    assert [e.type for e in events] == ["error"]
    assert events[0].message.diagnostics[0]["category"] == "authentication"
    assert events[0].message.diagnostics[0]["status"] == 401
    assert "invalid x-api-key [redacted]" in events[0].message.error
    assert "sk-ant-fixture-key" not in events[0].message.error


@pytest.mark.parametrize("tamper", [False, True])
async def test_late_reasoning_signature_backfill_allowed_without_weakening_content(tamper):
    import json

    before = {"id": "reasoning", "type": "reasoning", "summary": []}
    after = {**before, "encrypted_content": "opaque"}

    class Provider:
        name = "test"

        @event_contract
        async def stream(self, request, cancel):
            yield ModelEvent("start")
            yield ModelEvent.boundary("start", 0, ThinkingContent(""))
            yield ModelEvent.thinking("reason")
            yield ModelEvent.boundary("end", 0, ThinkingContent("reason", json.dumps(before)))
            yield ModelEvent.done(
                AssistantMessage(
                    [ThinkingContent("changed" if tamper else "reason", json.dumps(after))]
                )
            )

    result = await Agent(provider=Provider()).prompt("go")
    assert result.status == ("failed" if tamper else "completed")
    if not tamper:
        assert (
            json.loads(result.messages[-1].content[0].thinking_signature)["encrypted_content"]
            == "opaque"
        )


def test_proxy_usage_maps_known_fields_without_rewriting_private_payload():
    from pi_python.proxy import pi_message, pi_usage

    usage = {
        "input": 2,
        "cache_read": 3,
        "cache_write": 4,
        "total_tokens": 9,
        "cost": {"cache_read": 0.1},
    }
    message = AssistantMessage.text("done", usage=usage)
    wire = pi_message(message)
    assert wire["usage"]["cacheRead"] == 3 and wire["usage"]["cost"]["cacheRead"] == 0.1
    assert pi_usage(wire["usage"], decode=True) == usage
    private = ToolResultMessage("id", "name", [], details={"usage": usage})
    assert pi_message(private)["details"] == {"usage": usage}


async def test_agent_records_requested_thinking_for_custom_provider():
    agent = Agent(provider=ScriptedProvider([AssistantMessage.text("done")]), thinking_level="high")
    result = await agent.prompt("go")
    assert result.messages[-1].thinking_level == "high"
    assert result.messages[-1].provider_thinking_level is None


class CheckedStreaming:
    """A remote-style provider: event_contract turns failures into an error event."""

    name = "anthropic"

    def __init__(self, failure=None):
        self.failure = failure

    @event_contract
    async def stream(self, request, cancel):
        yield ModelEvent.boundary("start", 0, TextContent(""))
        yield ModelEvent.text("partial", 0)
        if self.failure:
            raise self.failure
        await asyncio.Event().wait()


@pytest.mark.parametrize("abort", [False, True])
async def test_checked_provider_failure_commits_its_partial_message(abort):
    finished = []
    provider = CheckedStreaming(None if abort else RuntimeError("overloaded"))
    agent = Agent(
        provider=provider,
        model="claude-x",
        hooks=Hooks(finish_turn=lambda ctx, cancel: finished.append(ctx.message.stop_reason)),
    )
    if abort:
        agent.subscribe(lambda e: agent.abort() if e.data.get("delta") == "partial" else None)
    result = await asyncio.wait_for(agent.prompt("go"), 5)
    message = result.messages[-1]
    expected = "aborted" if abort else "error"
    assert result.status == ("cancelled" if abort else "failed") and finished == [expected]
    assert message.stop_reason == expected and message.content[0].text == "partial"
    assert (message.provider, message.model) == ("anthropic", "claude-x")
    assert ("overloaded" in message.error) != abort
