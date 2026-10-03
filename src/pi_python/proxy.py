"""Pi agent-core streamProxy wire protocol (v1.0.0, MIT)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from .messages import Message
from .providers.transport import HTTPTransport
from typing import Any
from copy import deepcopy
import json
from .cancellation import CancelToken
from .errors import ConfigurationError, ProviderProtocolError, UnsupportedCapabilityError
from .messages import AssistantMessage, TextContent, ThinkingContent, ToolCall, message_to_dict
from .provider import ModelEvent, ModelRequest
from .tools import invoke
from .stream import event_contract
from .providers.common import RemoteProvider
import asyncio
from dataclasses import replace

_KEYS = {
    "tool_call": "toolCall",
    "tool_result": "toolResult",
    "tool_use": "toolUse",
    "text_signature": "textSignature",
    "thinking_signature": "thinkingSignature",
    "thought_signature": "thoughtSignature",
    "mime_type": "mimeType",
    "call_id": "toolCallId",
    "is_error": "isError",
    "stop_reason": "stopReason",
    "provider_thinking_level": "providerThinkingLevel",
    "tools_added": "toolsAdded",
    "tools_removed": "toolsRemoved",
    "input_schema": "parameters",
    "error": "errorMessage",
    "response_id": "responseId",
    "response_model": "responseModel",
    "thinking_level": "thinkingLevel",
    "raw_stop_reason": "rawStopReason",
    "end_turn": "endTurn",
    "nested_calls": "nestedCalls",
    "duration_ms": "durationMs",
    "arguments_bytes": "argumentsBytes",
    "model_id": "modelId",
    "expires_at": "expiresAt",
    "poll_after_ms": "pollAfterMs",
}
_OPTIONS = {
    "max_tokens": "maxTokens",
    "sampling_params": "samplingParams",
    "cache_retention": "cacheRetention",
    "session_id": "sessionId",
    "thinking_budgets": "thinkingBudgets",
    "max_retry_delay_ms": "maxRetryDelayMs",
}
_ALLOWED = {
    "temperature",
    "samplingParams",
    "maxTokens",
    "reasoning",
    "cacheRetention",
    "sessionId",
    "headers",
    "metadata",
    "transport",
    "thinkingBudgets",
    "maxRetryDelayMs",
}


def pi_usage(value: dict[str, Any], *, decode: bool = False) -> dict[str, Any]:
    keys = {
        "cache_read": "cacheRead",
        "cache_write": "cacheWrite",
        "cache_write_1h": "cacheWrite1h",
        "total_tokens": "totalTokens",
    }
    if decode:
        keys = {v: k for k, v in keys.items()}
    return {
        keys.get(k, k): pi_usage(v, decode=decode)
        if k == "cost" and isinstance(v, dict)
        else deepcopy(v)
        for k, v in value.items()
    }


def pi_message(message: Message) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, list):
            return [convert(v) for v in value]
        if isinstance(value, dict):
            result = {
                _KEYS.get(k, k): (
                    deepcopy(v)
                    if k
                    in {
                        "arguments",
                        "input_schema",
                        "data",
                        "usage",
                        "sections",
                        "details",
                        "structured_content",
                    }
                    else convert(v)
                )
                for k, v in value.items()
            }
            for key in ("type", "role", "stopReason"):
                if key in result:
                    result[key] = _KEYS.get(result[key], result[key])
            if value.get("role") in {"assistant", "tool_result"} and isinstance(
                value.get("usage"), dict
            ):
                result["usage"] = pi_usage(value["usage"])
            if value.get("role") == "tool_result":
                result["toolName"] = result.pop("name")
            if value.get("role") == "system":
                result["toolsRemoved"] = [{"name": name} for name in value.get("tools_removed", [])]
            if "timestamp" in result:
                result["timestamp"] *= 1000
            return result
        return value

    return convert(message_to_dict(message))


class ProxyProvider(RemoteProvider):
    name = "proxy"

    def __init__(
        self,
        *,
        model: dict[str, Any],
        proxy_url: str,
        auth_token: str | Callable[[], Any],
        transport: HTTPTransport | None = None,
    ) -> None:
        super().__init__(api_key=auth_token, transport=transport)
        if not {"id", "provider", "api"} <= model.keys():
            raise ConfigurationError(
                "Proxy model needs id, provider, api; use the server model descriptor"
            )
        self.model = deepcopy(model)
        self.proxy_url = proxy_url.rstrip("/")

    @event_contract
    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ModelEvent, None]:
        token = await self.credential(request, cancel)
        options = {
            _OPTIONS.get(k, k): v
            for k, v in request.options.items()
            if _OPTIONS.get(k, k) in _ALLOWED
        }
        # Transcript tool declarations are carried by system messages, as upstream does.
        messages = [pi_message(m) for m in request.messages]
        if request.tools:
            messages.append(
                {
                    "role": "system",
                    "content": "",
                    "sections": {},
                    "toolsAdded": [
                        {"name": t.name, "description": t.description, "parameters": t.input_schema}
                        for t in request.tools
                    ],
                    "toolsRemoved": [],
                    "timestamp": 0,
                }
            )
        body = await self.payload(
            request, {"model": self.model, "context": {"messages": messages}, "options": options}
        )
        events = self.transport.stream(
            self.proxy_url + "/api/stream",
            body,
            {"authorization": f"Bearer {token}", "content-type": "application/json"},
            cancel,
            on_response=request.on_response,
        )
        blocks: dict[int, Any] = {}
        arguments = {}
        final = None
        closed_blocks = set()
        yield ModelEvent("start")
        try:
            async for event in events:
                await invoke(request.on_provider_stream_event, deepcopy(event))
                kind = event.get("type")
                raw_index = event.get("contentIndex")
                index = raw_index if type(raw_index) is int else -1
                if str(kind).endswith(("_start", "_delta", "_end")) and index < 0:
                    raise ProviderProtocolError(f"Proxy {kind} without contentIndex")
                if final is not None:
                    raise ProviderProtocolError("Proxy event after completion")
                if kind in {"text_start", "thinking_start", "toolcall_start"}:
                    if type(index) is not int or index != len(blocks):
                        raise ProviderProtocolError("Invalid proxy block index")
                    if kind == "text_start":
                        blocks[index] = TextContent("")
                    elif kind == "thinking_start":
                        blocks[index] = ThinkingContent("")
                    else:
                        blocks[index] = ToolCall(event["id"], event["toolName"], {})
                        arguments[index] = ""
                    yield ModelEvent.boundary("start", index, blocks[index])
                elif (
                    kind in {"text_delta", "thinking_delta", "toolcall_delta"}
                    and index in closed_blocks
                ):
                    raise ProviderProtocolError("Proxy delta after block end")
                elif kind == "text_delta":
                    blocks[index].text += event["delta"]
                    yield ModelEvent.text(event["delta"], index)
                elif kind == "thinking_delta":
                    blocks[index].thinking += event["delta"]
                    yield ModelEvent.thinking(event["delta"], index)
                elif kind == "toolcall_delta":
                    arguments[index] += event["delta"]
                    b = blocks[index]
                    yield ModelEvent.toolcall(event["delta"], index)
                elif kind in {"text_end", "thinking_end"}:
                    if index in closed_blocks:
                        raise ProviderProtocolError("Duplicate proxy block end")
                    closed_blocks.add(index)
                    b = blocks[index]
                    if kind == "text_end":
                        b.text_signature = event.get("contentSignature")
                    else:
                        b.thinking_signature = event.get("contentSignature")
                    yield ModelEvent.boundary("end", index, b)
                elif kind == "toolcall_end":
                    if index in closed_blocks:
                        raise ProviderProtocolError("Duplicate proxy block end")
                    closed_blocks.add(index)
                    value = event["toolCall"]
                    b = blocks[index]
                    if value["id"] != b.id or value["name"] != b.name:
                        raise ProviderProtocolError("Proxy tool identity changed")
                    b.arguments = value["arguments"]
                    b.thought_signature = value.get("thoughtSignature")
                    b.namespace = value.get("namespace")
                    if arguments[index] and json.loads(arguments[index]) != b.arguments:
                        raise ProviderProtocolError("Proxy tool arguments mismatch")
                    yield ModelEvent.boundary("end", index, b)
                elif kind == "done":
                    if closed_blocks != set(blocks):
                        raise ProviderProtocolError("Proxy completed with unfinished blocks")
                    reason = {"stop": "stop", "length": "length", "toolUse": "tool_use"}.get(
                        event["reason"]
                    )
                    if reason is None:
                        raise ProviderProtocolError("Invalid proxy stop reason")
                    final = AssistantMessage(
                        list(blocks.values()),
                        reason,
                        self.model["provider"],
                        self.model["id"],
                        pi_usage(event.get("usage", {}), decode=True),
                        api=self.model["api"],
                        provider_thinking_level=event.get("providerThinkingLevel"),
                    )
                elif kind == "error":
                    if event.get("reason") == "aborted":
                        raise asyncio.CancelledError("Proxy request aborted")
                    raise ProviderProtocolError("Proxy stream reported an error")
                elif kind != "start":
                    raise UnsupportedCapabilityError(f"Unsupported proxy event: {kind}")
        finally:
            await events.aclose()
        if final is None:
            raise ProviderProtocolError("Proxy stream ended without completion")
        message_to_dict(final)
        yield ModelEvent.done(final)


def stream_proxy(
    model: dict[str, Any],
    context: ModelRequest | list[Message],
    options: dict[str, Any],
    cancel: CancelToken | None = None,
    *,
    transport: HTTPTransport | None = None,
) -> AsyncIterator[ModelEvent]:
    """Return an async iterator of ModelEvent; context is ModelRequest or message list."""
    options = dict(options)
    provider = ProxyProvider(
        model=model,
        proxy_url=options.pop("proxy_url"),
        auth_token=options.pop("auth_token"),
        transport=transport,
    )
    request = (
        context
        if isinstance(context, ModelRequest)
        else ModelRequest(context, model=model["id"], options=options)
    )
    if isinstance(context, ModelRequest):
        request = replace(context, options={**context.options, **options})
    return provider.stream(request, cancel or CancelToken())
