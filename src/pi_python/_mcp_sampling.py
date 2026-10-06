"""Lossless supported-subset conversion between MCP sampling and pi messages."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .errors import UnsupportedCapabilityError
from .messages import (
    AssistantMessage,
    ImageContent,
    Message,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolDeclaration,
    ToolResultMessage,
    UserMessage,
    message_to_dict,
)
from .provider import ModelRequest
from .transcript import current_system_prompt


def _only(data: dict[str, Any], allowed: set[str], label: str) -> None:
    if set(data) - allowed:
        raise UnsupportedCapabilityError(f"Unsupported MCP {label} fields")


def _blocks(content: Any) -> list[dict[str, Any]]:
    return content if isinstance(content, list) else [content]


def _from_content(block: dict[str, Any]) -> TextContent | ImageContent | ToolCall:
    kind = block.get("type")
    if kind == "text":
        _only(block, {"type", "text"}, "text")
        return TextContent(block["text"])
    if kind == "image":
        _only(block, {"type", "data", "mimeType"}, "image")
        return ImageContent(block["data"], block["mimeType"])
    if kind == "tool_use":
        _only(block, {"type", "id", "name", "input"}, "tool use")
        return ToolCall(block["id"], block["name"], deepcopy(block["input"]))
    raise UnsupportedCapabilityError(
        "Sampling supports text, input images and tool calls/results only"
    )


def _to_content(block: Any) -> dict[str, Any]:
    if isinstance(block, TextContent) and block.text_signature is None:
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageContent):
        return {"type": "image", "data": block.data, "mimeType": block.mime_type}
    if isinstance(block, ToolCall) and block.thought_signature is None and block.namespace is None:
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": deepcopy(block.arguments),
        }
    raise UnsupportedCapabilityError(
        "MCP sampling cannot represent this content or provider signature"
    )


def from_sampling(data: dict[str, Any]) -> ModelRequest:
    """Validate tool-result pairing before dispatching anything to a Provider."""
    _only(
        data,
        {
            "messages",
            "systemPrompt",
            "maxTokens",
            "temperature",
            "stopSequences",
            "metadata",
            "modelPreferences",
            "includeContext",
            "tools",
            "toolChoice",
            "_meta",
            "task",
        },
        "sampling request",
    )
    if data.get("includeContext") not in (None, "none"):
        raise UnsupportedCapabilityError("Sampling context inclusion is not enabled")
    if data.get("metadata") or data.get("stopSequences") or data.get("task") is not None:
        raise UnsupportedCapabilityError(
            "Sampling metadata, stop sequences and tasks are unsupported"
        )
    messages: list[Message] = []
    if data.get("systemPrompt"):
        messages.append(SystemMessage(data["systemPrompt"]))
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for message in data["messages"]:
        _only(message, {"role", "content"}, "message")
        blocks = _blocks(message["content"])
        results = [b for b in blocks if b["type"] == "tool_result"]
        if results:
            if message["role"] != "user" or len(results) != len(blocks):
                raise UnsupportedCapabilityError("Tool results must occupy their own user message")
            ids = [b["toolUseId"] for b in results]
            if len(set(ids)) != len(ids) or set(ids) != set(pending):
                raise UnsupportedCapabilityError("Tool results do not match pending tool call IDs")
            for result in results:
                _only(result, {"type", "toolUseId", "content", "isError"}, "tool result")
                content = [_from_content(b) for b in result.get("content", [])]
                if any(isinstance(b, ToolCall) for b in content):
                    raise UnsupportedCapabilityError("A tool result cannot contain a tool call")
                messages.append(
                    ToolResultMessage(
                        result["toolUseId"],
                        pending[result["toolUseId"]],
                        content,  # type: ignore[arg-type]
                        is_error=result.get("isError", False),
                    )
                )
            pending.clear()
            continue
        if pending:
            raise UnsupportedCapabilityError("Missing tool results before the next message")
        content = [_from_content(b) for b in blocks]
        if message["role"] == "user":
            if any(isinstance(b, ToolCall) for b in content):
                raise UnsupportedCapabilityError("Tool calls require an assistant message")
            messages.append(UserMessage(content))  # type: ignore[arg-type]
        else:
            if any(isinstance(b, ImageContent) for b in content):
                raise UnsupportedCapabilityError("Assistant images are not supported by pi-python")
            for block in content:
                if isinstance(block, ToolCall):
                    if not block.id or block.id in seen:
                        raise UnsupportedCapabilityError("Duplicate or empty tool call ID")
                    seen.add(block.id)
                    pending[block.id] = block.name
            messages.append(AssistantMessage(content, "tool_use" if pending else "stop"))  # type: ignore[arg-type]
    if pending:
        raise UnsupportedCapabilityError("Missing tool results at the end of sampling history")
    for message in messages:
        message_to_dict(message)
    declarations = []
    for tool in data.get("tools") or []:
        _only(tool, {"name", "description", "inputSchema"}, "tool declaration")
        declarations.append(
            ToolDeclaration(
                tool["name"], tool.get("description", ""), deepcopy(tool["inputSchema"])
            )
        )
    if len({t.name for t in declarations}) != len(declarations):
        raise UnsupportedCapabilityError("Duplicate sampling tool names")
    options = {"max_tokens": data["maxTokens"]}
    if data.get("temperature") is not None:
        options["temperature"] = data["temperature"]
    if data.get("toolChoice"):
        _only(data["toolChoice"], {"mode"}, "tool choice")
        options["tool_choice"] = data["toolChoice"]["mode"]
    return ModelRequest(messages, declarations, options=options)


def to_sampling(request: ModelRequest, max_tokens: int) -> dict[str, Any]:
    if request.api_key or any(
        (request.on_payload, request.on_response, request.on_provider_stream_event)
    ):
        raise UnsupportedCapabilityError(
            "SamplingProvider does not transport credentials or provider hooks"
        )
    if set(request.options) - {"max_tokens", "temperature", "tool_choice"}:
        raise UnsupportedCapabilityError(
            "SamplingProvider supports max_tokens, temperature and tool_choice only"
        )
    messages: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    for message in request.messages:
        if isinstance(message, SystemMessage):
            if isinstance(message.content, list):
                for block in message.content:
                    _to_content(block)
            continue  # Replay system sections with the same helper as direct Providers.
        if isinstance(message, ToolResultMessage):
            if call_names.get(message.call_id) != message.name:
                raise UnsupportedCapabilityError("Tool result name does not match its call ID")
            result = {
                "type": "tool_result",
                "toolUseId": message.call_id,
                "content": [_to_content(b) for b in message.content],
                "isError": message.is_error,
            }
            if (
                messages
                and messages[-1]["content"]
                and all(b["type"] == "tool_result" for b in messages[-1]["content"])
            ):
                messages[-1]["content"].append(result)
            else:
                messages.append({"role": "user", "content": [result]})
        elif isinstance(message, (UserMessage, AssistantMessage)):
            if isinstance(message, AssistantMessage):
                call_names.update((call.id, call.name) for call in message.tool_calls)
            blocks = (
                [TextContent(message.content)]
                if isinstance(message.content, str)
                else message.content
            )
            messages.append({"role": message.role, "content": [_to_content(b) for b in blocks]})
        else:
            raise UnsupportedCapabilityError("SamplingProvider cannot transport custom messages")
    data: dict[str, Any] = {
        "messages": messages,
        "maxTokens": request.options.get("max_tokens", max_tokens),
        "includeContext": "none",
    }
    prompt = current_system_prompt(request.messages)
    if prompt:
        data["systemPrompt"] = prompt
    if request.tools:
        data["tools"] = [
            {"name": t.name, "description": t.description, "inputSchema": deepcopy(t.input_schema)}
            for t in request.tools
        ]
    if "temperature" in request.options:
        data["temperature"] = request.options["temperature"]
    if "tool_choice" in request.options:
        choice = request.options["tool_choice"]
        if choice not in ("auto", "required", "none"):
            raise UnsupportedCapabilityError("Sampling tool_choice must be auto, required or none")
        data["toolChoice"] = {"mode": choice}
    from_sampling(data)  # The same pairing and supported-content checks in both directions.
    return data


def to_sampling_result(message: AssistantMessage, tools: bool, model: str) -> Any:
    from mcp import types

    if message.error or message.stop_reason not in {"stop", "length", "tool_use"}:
        raise UnsupportedCapabilityError("Provider did not return a successful sampling response")
    content = [_to_content(b) for b in message.content]
    if not tools and (len(content) != 1 or content[0]["type"] != "text"):
        raise UnsupportedCapabilityError("Basic sampling requires a single text response")
    cls = types.CreateMessageResultWithTools if tools else types.CreateMessageResult
    return cls.model_validate(
        {
            "role": "assistant",
            "content": content if tools else content[0],
            "model": model,
            "stopReason": {"stop": "endTurn", "length": "maxTokens", "tool_use": "toolUse"}[
                message.stop_reason
            ],
        }
    )


def from_sampling_result(data: dict[str, Any]) -> AssistantMessage:
    _only(data, {"role", "content", "model", "stopReason"}, "sampling response")
    if data["role"] != "assistant":
        raise UnsupportedCapabilityError("SamplingProvider requires an assistant response")
    content = [_from_content(b) for b in _blocks(data["content"])]
    if any(isinstance(b, ImageContent) for b in content):
        raise UnsupportedCapabilityError("Assistant image responses are unsupported")
    calls = [b for b in content if isinstance(b, ToolCall)]
    if len({b.id for b in calls}) != len(calls):
        raise UnsupportedCapabilityError("Duplicate tool call IDs in sampling response")
    reason = data.get("stopReason", "endTurn")
    if reason not in {"endTurn", "stopSequence", "maxTokens", "toolUse"}:
        raise UnsupportedCapabilityError("Unsupported sampling stop reason")
    if bool(calls) != (reason == "toolUse"):
        raise UnsupportedCapabilityError("Sampling stop reason does not match tool calls")
    return AssistantMessage(
        content,  # type: ignore[arg-type]
        stop_reason={
            "endTurn": "stop",
            "stopSequence": "stop",
            "maxTokens": "length",
            "toolUse": "tool_use",
        }[reason],
        provider="mcp-sampling",
        model=data["model"],
    )
