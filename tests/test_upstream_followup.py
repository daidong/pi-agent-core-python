"""Regressions selected from Pi v1.4.0 and explicitly pinned later fixes."""

import asyncio
import time

import pytest
import httpx

from pi_python import (
    AssistantMessage,
    CancelToken,
    ImageContent,
    ModelInfo,
    ModelRequest,
    TextContent,
    ThinkingContent,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    is_context_overflow,
    is_retryable_error,
)
from pi_python.providers import AnthropicProvider, HTTPTransport, ProviderHTTPError
from pi_python.providers.oauth import OAuthCredential, RefreshingCredentials
from pi_python.providers.openai import _tool_output
from pi_python.providers.common import transform_messages
from pi_python.plugins._resources import parse_command_args, substitute_args


@pytest.mark.parametrize(
    "error,expected",
    [
        ("invalid request req_011CU503xyz", False),
        ("invalid request: 5242880 bytes", False),
        ("503 Service Unavailable", True),
        ("The service is temporarily unavailable", True),
        ("Unable to forward upstream stream", True),
        ("upstream_stream_read_error", True),
        ("stream disconnected before completion", True),
        ("Selected model is at capacity", True),
        ("server_busy", True),
        ("429 subscription_sharing_workplace_individual_usage_limit_exceeded", False),
    ],
)
def test_upstream_retry_classification(error, expected):
    assert is_retryable_error(AssistantMessage([], stop_reason="error", error=error)) is expected


@pytest.mark.parametrize(
    "error,expected",
    [
        ("OpenAI API error (413): failed to parse request", True),
        ("Provider request failed (HTTP 413; request): failed to parse request", True),
        ("413 failed to parse request", True),
        ("litellm.ContextWindowExceededError: Context Window exceeded for given call.", True),
        ("bad request with id 413", False),
        ("Provider request failed (HTTP 400; request): id 413", False),
    ],
)
def test_upstream_overflow_classification(error, expected):
    assert is_context_overflow(AssistantMessage([], stop_reason="error", error=error)) is expected


@pytest.mark.parametrize("cancel_kind", ["token", "task"])
async def test_refresh_finishes_and_persists_before_cancellation_returns(cancel_kind):
    started, release = asyncio.Event(), asyncio.Event()
    old = OAuthCredential("anthropic", "fixture-old", "fixture-refresh-old", 0)
    new = OAuthCredential("anthropic", "fixture-new", "fixture-refresh-new", time.time() + 3600)
    saved, refresh_tokens = [], []

    class Client:
        async def refresh(self, credential, cancel):
            refresh_tokens.append(cancel)
            started.set()  # Simulate a server that has already rotated the token.
            await release.wait()
            cancel.raise_if_cancelled()
            return new

    store = RefreshingCredentials(old, client=Client(), persist=saved.append)
    token = CancelToken()
    task = asyncio.create_task(store.get(token))
    await started.wait()
    if cancel_kind == "token":
        token.cancel()
    else:
        task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.credential == new and saved == [new]
    assert not refresh_tokens[0].cancelled
    assert await store.get() == new


@pytest.mark.parametrize("text", ["", "\r\n", " \t "])
def test_responses_blank_tool_output(text):
    assert _tool_output([TextContent(text)], False) == "(no tool output)"
    assert _tool_output([TextContent(text), ImageContent("AAAA", "image/png")], True) == [
        {"type": "input_image", "detail": "auto", "image_url": "data:image/png;base64,AAAA"}
    ]


async def test_anthropic_error_images_follow_all_tool_results():
    provider = AnthropicProvider(api_key="fixture")
    model = ModelInfo(
        "test", "anthropic", "anthropic-messages", "Test", 100000, 4096, input=("text", "image")
    )
    messages = [
        UserMessage("test"),
        AssistantMessage(
            [ToolCall("a", "first", {}), ToolCall("b", "second", {})],
            stop_reason="tool_use",
            provider="anthropic",
            api=model.api,
            model=model.id,
        ),
        ToolResultMessage("a", "first", [ImageContent("AAAA", "image/png")], is_error=True),
        ToolResultMessage("b", "second", [TextContent("ok")]),
    ]
    try:
        body, _, _ = provider._build(ModelRequest(messages, model=model.id, model_info=model))
    finally:
        await provider.aclose()
    blocks = body["messages"][-1]["content"]
    assert [b["type"] for b in blocks] == ["tool_result", "tool_result", "text", "image"]
    assert blocks[0]["is_error"] is True
    assert blocks[0]["content"] == "(image attached after the tool results)"
    assert "a (first)" in blocks[2]["text"]
    assert messages[2].content[0].type == "image"  # Stored history is unchanged.


async def test_workspace_limit_is_not_retried_by_http_transport():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            429,
            headers={"Retry-After": "0"},
            json={
                "error": {"code": "subscription_sharing_workplace_individual_usage_limit_exceeded"}
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = HTTPTransport(client, max_retries=2)
        with pytest.raises(ProviderHTTPError) as caught:
            async for _ in transport.stream("https://fixture.test", {}, {}, CancelToken()):
                pass
    assert not caught.value.retryable
    assert len(requests) == 1


async def test_cancelled_refresh_persistence_keeps_lock_and_retries_failed_save():
    saving, release = asyncio.Event(), asyncio.Event()
    old = OAuthCredential("anthropic", "fixture-old", "fixture-refresh-old", 0)
    new = OAuthCredential("anthropic", "fixture-new", "fixture-refresh-new", time.time() + 3600)
    calls, saved = [], []

    class Client:
        async def refresh(self, credential, cancel):
            calls.append(credential)
            return new

    async def persist(credential):
        saved.append(credential)
        if len(saved) == 1:
            saving.set()
            await release.wait()
            raise OSError("disk temporarily unavailable")

    store = RefreshingCredentials(old, client=Client(), persist=persist)
    task = asyncio.create_task(store.get())
    await saving.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # repeated cancellation cannot release the rotation lock early
    assert store._lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.credential == new
    assert await store.get() == new
    assert calls == [old] and saved == [new, new]


def test_switching_models_drops_reasoning_without_mutating_history():
    history = [
        AssistantMessage(
            [ThinkingContent("private reasoning"), TextContent("visible answer")],
            provider="old",
            api="old",
            model="old",
        )
    ]
    replay = transform_messages(history, "new", "new", "new")
    assert replay[0].content == [TextContent("visible answer")]
    assert isinstance(history[0].content[0], ThinkingContent)
    same = transform_messages(history, "old", "old", "old")
    assert same[0].content == history[0].content


@pytest.mark.parametrize("text", ['"" second', "'' second", '"" "second"'])
def test_empty_quoted_template_argument_keeps_position(text):
    args = parse_command_args(text)
    assert args == ["", "second"]
    assert substitute_args("${1:-default}/$2", args) == "default/second"


async def test_anthropic_system_update_waits_past_empty_assistant():
    provider = AnthropicProvider(api_key="fixture")
    model = ModelInfo(
        "test",
        "anthropic",
        "anthropic-messages",
        "Test",
        100000,
        4096,
        compat={"supportsMidConvoSystemMessages": True},
    )
    request = ModelRequest(
        [
            SystemMessage("initial"),
            UserMessage("one"),
            AssistantMessage.text("answer", provider="anthropic", api=model.api, model=model.id),
            SystemMessage("update"),
            AssistantMessage([], provider="anthropic", api=model.api, model=model.id),
            UserMessage("two"),
        ],
        model=model.id,
        model_info=model,
    )
    try:
        body, _, _ = provider._build(request)
    finally:
        await provider.aclose()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user", "system"]
