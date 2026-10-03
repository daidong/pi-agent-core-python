"""OpenAI Responses API and Codex subscription transport, ported from Pi v1.0.0."""

from __future__ import annotations
from collections.abc import AsyncGenerator
from ..cancellation import CancelToken
from ..provider import ModelRequest
from ..messages import Message, ToolDeclaration
from ..models import ModelInfo
from typing import Any
import base64
import json
from copy import deepcopy
from ..errors import ConfigurationError, ProviderProtocolError, UnsupportedCapabilityError
from ..messages import (
    AssistantMessage,
    UserMessage,
    ToolResultMessage,
    TextContent,
    ImageContent,
    ThinkingContent,
    ToolCall,
    CustomMessage,
)
from ..provider import ModelEvent
import re
from ..messages import SystemMessage
from ..estimate import clamp_max_tokens_to_context, short_hash
from ..transcript import (
    current_system_prompt,
    initial_system_message,
    render_system_update,
    resolve_transcript,
    resolve_transcript_tools,
    system_message_text,
    with_request_tools,
)
from ..tools import invoke
from ..stream import event_contract
from .common import RemoteProvider, normalize_usage, transform_messages
from .transport import WebSocketLink
from .._version import __version__


def input_content(content: str | list[TextContent | ImageContent]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    return [
        (
            {"type": "input_text", "text": b.text}
            if isinstance(b, TextContent)
            else {
                "type": "input_image",
                "image_url": f"data:{b.mime_type};base64,{b.data}",
                "detail": "auto",
            }
        )
        for b in content
    ]


def account_id(token: str) -> str:
    """Read an account routing hint, not an identity or signature verification."""
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        value = payload["https://api.openai.com/auth"]["chatgpt_account_id"]
        if not isinstance(value, str) or not value:
            raise ValueError
        return value
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ConfigurationError("Codex OAuth token lacks an account routing ID") from exc


def response_block(item: dict[str, Any]) -> TextContent | ThinkingContent | ToolCall:
    blocks: list[TextContent | ThinkingContent | ToolCall] = []
    kind = item["type"]
    if kind == "message":
        text = "".join(c.get("text", c.get("refusal", "")) for c in item.get("content", []))
        signature = {"v": 1, "id": item["id"]}
        if item.get("phase"):
            signature["phase"] = item["phase"]
        blocks.append(TextContent(text, json.dumps(signature, separators=(",", ":"))))
    elif kind == "reasoning":
        text = "\n\n".join(c["text"] for c in (item.get("summary") or item.get("content") or []))
        blocks.append(
            ThinkingContent(text, json.dumps(item, separators=(",", ":"), ensure_ascii=False))
        )
    elif kind == "function_call":
        arguments = json.loads(item["arguments"])
        if not isinstance(arguments, dict):
            raise ProviderProtocolError("Tool arguments must be an object")
        blocks.append(
            ToolCall(
                item["call_id"] + "|" + item["id"],
                item["name"],
                arguments,
                namespace=item.get("namespace"),
            )
        )
    else:
        raise UnsupportedCapabilityError(f"Unsupported OpenAI output item: {kind}")
    return blocks[0]


_TOOL_CALL_PROVIDERS = {"openai", "openai-codex", "opencode"}


def _id_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)[:64].rstrip("_")


def _text_signature(signature: str | None) -> dict[str, str] | None:
    """Pi parseTextSignature: a TextSignatureV1 JSON object or a legacy item ID."""
    if not signature:
        return None
    if signature.startswith("{"):
        try:
            parsed = json.loads(signature)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("v") == 1 and isinstance(parsed.get("id"), str):
            if parsed.get("phase") in {"commentary", "final_answer"}:
                return {"id": parsed["id"], "phase": parsed["phase"]}
            return {"id": parsed["id"]}
    return {"id": signature}


def _tool_output(
    content: list[TextContent | ImageContent], images_allowed: bool
) -> str | list[dict[str, Any]]:
    text = "\n".join(b.text for b in content if isinstance(b, TextContent))
    images = [b for b in content if isinstance(b, ImageContent)]
    if not images or not images_allowed:
        return text or ("(see attached image)" if images else "(no tool output)")
    output: list[dict[str, Any]] = [{"type": "input_text", "text": text}] if text else []
    return output + [
        {
            "type": "input_image",
            "detail": "auto",
            "image_url": f"data:{b.mime_type};base64,{b.data}",
        }
        for b in images
    ]


class OpenAIProvider(RemoteProvider):
    name = "openai"
    api = "openai-responses"

    def __init__(self, *, base_url: str = "https://api.openai.com/v1", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_url = base_url.rstrip("/")

    def _tools(
        self, tools: list[ToolDeclaration], compat: dict[str, Any], strict: bool | None
    ) -> list[dict[str, Any]]:
        supports_strict = compat.get("supportsStrictMode", self.name == "openai-codex")
        return [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": deepcopy(t.input_schema),
                **({"strict": strict} if supports_strict else {}),
            }
            for t in tools
        ]

    def _input(
        self,
        request: ModelRequest,
        model: ModelInfo,
        transcript: list[Message],
        compat: dict[str, Any],
        *,
        include_system: bool,
        strict: bool | None,
    ) -> tuple[list[dict[str, Any]], list[Message]]:
        """Pi convertResponsesMessages for this provider's model."""
        mid = compat.get("supportsMidConvoSystemMessages") is True
        transcript = resolve_transcript(transcript, mid)
        allowed = self.name in _TOOL_CALL_PROVIDERS

        def normalize(value: str, source: AssistantMessage) -> str:
            if not allowed or "|" not in value:
                return _id_part(value)
            call, item = value.split("|")[:2]
            foreign = source.provider != self.name or source.api != self.api
            item = ("fc_" + short_hash(item))[:64] if foreign else _id_part(item)
            if not item.startswith("fc_"):
                item = _id_part("fc_" + item)
            return f"{_id_part(call)}|{item}"

        transformed = transform_messages(transcript, self.name, self.api, request.model, normalize)
        additional = compat.get("supportsAdditionalTools") is True
        search = compat.get("supportsToolSearch") is True
        _, anchors = resolve_transcript_tools(transcript, additional or search)
        role = (
            "developer"
            if model.reasoning and compat.get("supportsDeveloperRole") is not False
            else "system"
        )
        images_allowed = "image" in model.input
        items: list[dict[str, Any]] = []
        position = 0
        for source, message in enumerate(transformed):
            leading = source == 0 and isinstance(message, SystemMessage)
            if isinstance(message, SystemMessage):
                if not leading and anchors and message.tools_added:
                    tools = message.tools_added
                    if additional:
                        items.append(
                            {
                                "type": "additional_tools",
                                "role": "developer",
                                "tools": self._tools(tools, compat, strict),
                            }
                        )
                    elif search:
                        # Client-executed tool search loads late tools where they appear.
                        names = [t.name for t in tools]
                        call = "pi_tool_load_" + short_hash(f"system:{position}:{','.join(names)}")
                        items.append(
                            {
                                "type": "tool_search_call",
                                "call_id": call,
                                "execution": "client",
                                "status": "completed",
                                "arguments": {"query": " ".join(names), "limit": len(names)},
                            }
                        )
                        items.append(
                            {
                                "type": "tool_search_output",
                                "call_id": call,
                                "execution": "client",
                                "status": "completed",
                                "tools": [
                                    {**t, "defer_loading": True}
                                    for t in self._tools(tools, compat, strict)
                                ],
                            }
                        )
                if not leading or include_system:
                    text = (
                        system_message_text(message) if leading else render_system_update(message)
                    )
                    if text:
                        items.append({"role": role, "content": text})
            elif isinstance(message, UserMessage):
                content = input_content(message.content)
                if not content:
                    continue
                items.append({"role": "user", "content": content})
            elif isinstance(message, AssistantMessage):
                output: list[dict[str, Any]] = []
                same_api = message.provider == self.name and message.api == self.api
                same = same_api and message.model == request.model
                text_index = 0
                for b in message.content:
                    if isinstance(b, ThinkingContent):
                        if b.thinking_signature:
                            item = json.loads(b.thinking_signature)
                            if not isinstance(item, dict) or item.get("type") != "reasoning":
                                raise ProviderProtocolError("Invalid reasoning replay item")
                            output.append(item)
                    elif isinstance(b, TextContent):
                        signature = _text_signature(b.text_signature)
                        fallback = (
                            f"msg_pi_{position}"
                            if text_index == 0
                            else f"msg_pi_{position}_{text_index}"
                        )
                        text_index += 1
                        identifier = signature["id"] if signature else fallback
                        if len(identifier) > 64:
                            identifier = "msg_" + short_hash(identifier)
                        item = {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": b.text, "annotations": []}],
                            "status": "completed",
                            "id": identifier,
                        }
                        if signature and signature.get("phase"):
                            item["phase"] = signature["phase"]
                        output.append(item)
                    elif isinstance(b, ToolCall):
                        call, _, item_id = b.id.partition("|")
                        item = {
                            "type": "function_call",
                            "call_id": call,
                            "name": b.name,
                            "arguments": json.dumps(
                                b.arguments, ensure_ascii=False, separators=(",", ":")
                            ),
                        }
                        # A different model's item ID would trip reasoning pairing validation.
                        if not (same_api and not same) and item_id.startswith("fc_"):
                            item = {"type": "function_call", "id": item_id, **item}
                        if same and b.namespace is not None:
                            item["namespace"] = b.namespace
                        output.append(item)
                if not output:
                    continue
                items += output
            elif isinstance(message, ToolResultMessage):
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.call_id.split("|")[0],
                        "output": _tool_output(message.content, images_allowed),
                    }
                )
            elif isinstance(message, CustomMessage):
                raise UnsupportedCapabilityError("Convert custom messages before provider boundary")
            if not leading:
                position += 1
        return items, transcript

    def build_request(self, request: ModelRequest, chatgpt_sign_in: bool = False) -> dict[str, Any]:
        model = self.model_info(request)
        compat = model.compat
        options = request.options
        source = with_request_tools(request.messages, request.tools)
        codex = self.name == "openai-codex"
        strict = None if codex else False
        items, transcript = self._input(
            request, model, source, compat, include_system=not codex, strict=strict
        )
        additional = compat.get("supportsAdditionalTools") is True
        search = compat.get("supportsToolSearch") is True
        tools, _ = resolve_transcript_tools(transcript, additional or search)
        retention = options.get("cache_retention", "short")
        if retention not in {"none", "short", "long"}:
            raise ConfigurationError("Invalid cache_retention")
        key = options.get("session_id") if retention != "none" else None
        if isinstance(key, str) and len(key) > 64:
            key = key[:64]
        body: dict[str, Any] = {
            "model": request.model,
            "input": items,
            "stream": True,
            "store": False,
        }
        if key:
            body["prompt_cache_key"] = key
        reasoning = options.get("reasoning")
        if reasoning == "off":
            reasoning = None
        if reasoning is not None:
            if reasoning not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
                raise ConfigurationError("Unsupported reasoning effort")
            reasoning = model.clamp_thinking_level(reasoning)
            if reasoning == "off":
                reasoning = None
        level_map = model.thinking_level_map
        if codex:
            initial = initial_system_message(transcript)
            body["instructions"] = (
                system_message_text(initial) if initial else ""
            ) or "You are a helpful assistant."
            body["text"] = deepcopy(options.get("text", {"verbosity": "low"}))
            body["include"] = ["reasoning.encrypted_content"]
            body["tool_choice"] = deepcopy(options.get("tool_choice", "auto"))
            body["parallel_tool_calls"] = options.get("parallel_tool_calls", True)
            if reasoning is not None:
                effort = level_map.get(reasoning) or reasoning
                body["reasoning"] = {
                    "effort": effort,
                    "summary": options.get("reasoning_summary", "auto"),
                }
            elif model.reasoning and level_map.get("off", "") is not None:
                body["reasoning"] = {"effort": level_map.get("off") or "none"}
        else:
            explicit = compat.get("supportsExplicitPromptCacheMode") is True
            long_ok = compat.get("supportsLongCacheRetention", True)
            if not chatgpt_sign_in:
                if retention == "long" and long_ok and not explicit:
                    body["prompt_cache_retention"] = "24h"
                if explicit and retention == "none":
                    body["prompt_cache_options"] = {"mode": "explicit"}
                elif explicit and retention == "long" and long_ok:
                    body["prompt_cache_options"] = {"ttl": "30m"}
            max_tokens = clamp_max_tokens_to_context(
                model.context_window, source, options.get("max_tokens", model.max_tokens)
            )
            if max_tokens and compat.get("supportsMaxOutputTokens", True) and not chatgpt_sign_in:
                body["max_output_tokens"] = max(max_tokens, 16)
            if model.reasoning:
                if reasoning is not None or options.get("reasoning_summary"):
                    effort = (level_map.get(reasoning) or reasoning) if reasoning else "medium"
                    body["reasoning"] = {
                        "effort": effort,
                        "summary": options.get("reasoning_summary") or "auto",
                    }
                    body["include"] = ["reasoning.encrypted_content"]
                elif level_map.get("off", "") is not None:
                    body["reasoning"] = {"effort": level_map.get("off") or "none"}
        if tools:
            body["tools"] = self._tools(tools, compat, strict)
        for key_name in (
            "temperature",
            "top_p",
            "tool_choice",
            "parallel_tool_calls",
            "metadata",
            "service_tier",
            "text",
            "include",
        ):
            if key_name in options and not (chatgpt_sign_in and key_name == "temperature"):
                body[key_name] = deepcopy(options[key_name])
        return body

    @event_contract
    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ModelEvent, None]:
        key = await self.credential(request, cancel)
        headers = {
            **request.options.get("headers", {}),
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        transport = request.options.get("transport", "sse")
        if transport not in {"sse", "websocket", "websocket-cached", "auto"}:
            raise ConfigurationError("transport must be sse, websocket, websocket-cached or auto")
        if self.name == "openai-codex":
            headers.update(
                {
                    "chatgpt-account-id": account_id(key),
                    "originator": "pi",
                    "user-agent": f"pi-python/{__version__}",
                    "OpenAI-Beta": "responses=experimental",
                }
            )
            if (
                request.options.get("session_id")
                and request.options.get("cache_retention") != "none"
            ):
                headers["session-id"] = headers["x-client-request-id"] = request.options[
                    "session_id"
                ][:64]
        if (
            self.name == "openai"
            and request.options.get("session_id")
            and request.options.get("cache_retention") != "none"
        ):
            headers["x-client-request-id"] = request.options["session_id"]
        # Sign in with ChatGPT rejects some request fields that API keys accept.
        chatgpt = (
            self.name == "openai"
            and self.base_url == "https://api.openai.com/v1"
            and not key.startswith("sk-")
        )
        body = await self.payload(request, self.build_request(request, chatgpt))
        if transport == "websocket-cached" and not request.options.get("session_id"):
            raise ConfigurationError("websocket-cached requires session_id")
        # Pi caches a connection per session unless caching is disabled for the request.
        cached = (
            request.options.get("session_id")
            if request.options.get("cache_retention") != "none"
            else None
        )
        # Codex keeps connection-scoped response state, so a cached socket can continue
        # from the previous response instead of resending the whole context.
        link = (
            WebSocketLink()
            if self.name == "openai-codex" and cached and transport in {"websocket-cached", "auto"}
            else None
        )
        events = self.transport.responses(
            self.base_url + "/responses",
            body,
            headers,
            cancel,
            mode=transport,
            session_id=cached,
            on_response=request.on_response,
            **({"link": link} if link is not None else {}),
        )
        slots: dict[int, Any] = {}
        final_response = None
        announced = False
        try:
            async for event in events:
                if not announced:
                    announced = True
                    yield ModelEvent("start")
                await invoke(request.on_provider_stream_event, deepcopy(event))
                kind = event.get("type", "")
                if kind == "transport_done":
                    continue
                if final_response is not None:
                    raise ProviderProtocolError("OpenAI event after completion")
                if kind in {"error", "response.failed"}:
                    raise ProviderProtocolError("OpenAI stream reported an error")
                raw_index = event.get("output_index")
                index = raw_index if type(raw_index) is int else -1
                if index < 0 and (
                    kind.startswith(("response.output_item.", "response.reasoning_summary_part."))
                    or kind.endswith(".delta")
                ):
                    raise ProviderProtocolError(f"OpenAI {kind} without output_index")
                if kind == "response.output_item.added":
                    item = event["item"]
                    if index in slots:
                        raise ProviderProtocolError("Duplicate OpenAI output item")
                    slots[index] = {
                        "item": item,
                        "index": len(slots),
                        "text": "",
                        "thinking": "",
                        "arguments": item.get("arguments", ""),
                    }
                    initial = (
                        response_block({**item, "arguments": "{}"})
                        if item["type"] == "function_call"
                        else TextContent("")
                        if item["type"] == "message"
                        else ThinkingContent("")
                        if item["type"] == "reasoning"
                        else None
                    )
                    if initial is None:
                        raise UnsupportedCapabilityError(
                            f"Unsupported OpenAI output item: {item['type']}"
                        )
                    yield ModelEvent.boundary("start", slots[index]["index"], initial)
                    if item.get("arguments") and item.get("type") == "function_call":
                        yield ModelEvent.toolcall(item["arguments"], slots[index]["index"])
                elif kind in {
                    "response.output_text.delta",
                    "response.refusal.delta",
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                    "response.function_call_arguments.delta",
                }:
                    if index not in slots:
                        raise ProviderProtocolError("Delta without output item")
                    slot = slots[index]
                    delta = event["delta"]
                    if kind in {"response.output_text.delta", "response.refusal.delta"}:
                        slot["text"] += delta
                        yield ModelEvent.text(delta, slot["index"])
                    elif kind == "response.function_call_arguments.delta":
                        slot["arguments"] += delta
                        yield ModelEvent.toolcall(delta, slot["index"])
                    else:
                        slot["thinking"] += delta
                        yield ModelEvent.thinking(delta, slot["index"])
                elif (
                    kind == "response.reasoning_summary_part.added"
                    and event.get("summary_index", 0) > 0
                ):
                    slot = slots[index]
                    slot["thinking"] += "\n\n"
                    yield ModelEvent.thinking("\n\n", slot["index"])
                elif kind == "response.output_item.done":
                    if index not in slots:
                        slots[index] = {
                            "index": len(slots),
                            "text": "",
                            "thinking": "",
                            "arguments": "",
                        }
                    slots[index]["item"] = event["item"]
                    yield ModelEvent.boundary(
                        "end", slots[index]["index"], response_block(event["item"])
                    )
                elif kind in {"response.completed", "response.incomplete", "response.done"}:
                    final_response = event["response"]
        finally:
            await events.aclose()
        if final_response is None:
            raise ProviderProtocolError("OpenAI stream ended without completion")
        output = final_response.get("output") or [s["item"] for s in slots.values()]
        blocks: list[TextContent | ThinkingContent | ToolCall] = []
        for item in output:
            block = response_block(item)
            if isinstance(block, ThinkingContent) and item.get("encrypted_content"):
                old = next(
                    (
                        slot["item"]
                        for slot in slots.values()
                        if slot["item"].get("id") == item.get("id")
                    ),
                    None,
                )
                if old is not None and not old.get("encrypted_content"):
                    block.thinking_signature = json.dumps(
                        {**old, "encrypted_content": item["encrypted_content"]},
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
            blocks.append(block)
        status = final_response.get("status", "completed")
        if status not in {"completed", "incomplete"}:
            raise ProviderProtocolError(f"OpenAI response status: {status}")
        if (
            status == "incomplete"
            and final_response.get("incomplete_details", {}).get("reason") != "max_output_tokens"
        ):
            raise ProviderProtocolError("OpenAI response incomplete")
        reason = (
            "length"
            if status == "incomplete"
            else ("tool_use" if any(isinstance(b, ToolCall) for b in blocks) else "stop")
        )
        message = AssistantMessage(
            blocks,
            reason,
            self.name,
            request.model,
            normalize_usage(final_response.get("usage", {}), self.name),
            api=self.api,
            thinking_level=request.options.get("reasoning"),
            response_id=final_response.get("id"),
            response_model=final_response.get("model")
            if final_response.get("model") != request.model
            else None,
            raw_stop_reason=status + ".max_output_tokens" if status == "incomplete" else status,
        )
        if link is not None and link.entry is not None and final_response.get("id"):
            model = self.model_info(request)
            items, _ = self._input(
                request,
                model,
                [message],
                model.compat,
                include_system=False,
                strict=None,
            )
            link.entry.continuation = {
                "body": body,
                "response_id": final_response["id"],
                "items": [i for i in items if i.get("type") != "function_call_output"],
            }
        yield ModelEvent.done(message)


class OpenAICodexProvider(OpenAIProvider):
    name = "openai-codex"
    api = "openai-codex-responses"

    def __init__(
        self, *, base_url: str = "https://chatgpt.com/backend-api/codex", **kwargs: Any
    ) -> None:
        super().__init__(base_url=base_url, **kwargs)


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek's documented stateless Responses API compatibility profile."""

    name = "deepseek"

    def __init__(self, *, base_url: str = "https://api.deepseek.com", **kwargs: Any) -> None:
        super().__init__(base_url=base_url, **kwargs)

    def build_request(self, request: ModelRequest, chatgpt_sign_in: bool = False) -> dict[str, Any]:
        body = super().build_request(request)
        system = current_system_prompt(request.messages)
        # The replayed prompt travels in instructions; the instruction role is not used.
        body["input"] = [
            item for item in body["input"] if item.get("role") not in {"developer", "system"}
        ]
        if system:
            body["instructions"] = system
        level = request.options.get("reasoning", "off")
        body["reasoning"] = {
            "effort": {"off": "none", "minimal": "low", "medium": "high", "xhigh": "high"}.get(
                level, level
            )
        }
        for key in ("include", "metadata", "service_tier", "prompt_cache_key"):
            body.pop(key, None)
        if "max_tokens" not in request.options:
            body.pop("max_output_tokens", None)
        return body
