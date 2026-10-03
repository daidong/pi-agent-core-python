from __future__ import annotations
from collections.abc import Callable
from typing import Any
from ..cancellation import CancelToken
from ..messages import Message
from ..models import ModelCatalog

import time
from copy import deepcopy
from ..errors import ConfigurationError
from ..models import ModelInfo
from ..provider import ModelRequest
from ..messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from ..tools import invoke
from .transport import HTTPTransport


def same_model(message: AssistantMessage, provider: str, api: str, model: str) -> bool:
    return message.provider == provider and message.api == api and message.model == model


def transform_messages(
    messages: list[Message],
    provider: str,
    api: str,
    model: str,
    normalize_id: Callable[[str, AssistantMessage], str] | None = None,
) -> list[Message]:
    """Pi transformMessages: replay-safe history for one target model.

    Cross-model thinking becomes text and opaque signatures are dropped. Failed or
    aborted assistant turns are not replayed. A system message between a tool call
    and its results moves after them. Python history validation already rejects
    dangling calls; the synthetic result only covers hand-built requests.
    """
    ids: dict[str, str] = {}
    first = []
    for message in deepcopy(messages):
        if isinstance(message, ToolResultMessage):
            message.call_id = ids.get(message.call_id, message.call_id)
        elif isinstance(message, AssistantMessage):
            same = same_model(message, provider, api, model)
            content: list[TextContent | ThinkingContent | ToolCall] = []
            for block in message.content:
                if isinstance(block, ThinkingContent):
                    if block.redacted:
                        if same:
                            content.append(block)
                    elif same and block.thinking_signature:
                        content.append(block)
                    elif block.thinking.strip():
                        content.append(block if same else TextContent(block.thinking))
                elif isinstance(block, TextContent):
                    content.append(block if same else TextContent(block.text))
                elif isinstance(block, ToolCall):
                    if not same:
                        block.thought_signature = None
                        if normalize_id is not None:
                            new = normalize_id(block.id, message)
                            if new != block.id:
                                ids[block.id] = new
                                block.id = new
                    content.append(block)
            message.content = content
        first.append(message)
    result: list = []
    pending: list[ToolCall] = []
    answered: set[str] = set()
    held: list[SystemMessage] = []

    def close() -> None:
        nonlocal pending, answered
        for call in pending:
            if call.id not in answered:
                result.append(
                    ToolResultMessage(call.id, call.name, [TextContent("No result provided")], True)
                )
        pending, answered = [], set()
        result.extend(held)
        held.clear()

    for message in first:
        if isinstance(message, AssistantMessage):
            close()
            if message.stop_reason in {"error", "aborted"}:
                continue
            if message.tool_calls:
                pending, answered = message.tool_calls, set()
            result.append(message)
        elif isinstance(message, ToolResultMessage):
            answered.add(message.call_id)
            result.append(message)
        elif isinstance(message, SystemMessage) and pending:
            held.append(message)
        else:
            if isinstance(message, UserMessage):
                close()
            result.append(message)
    close()
    return result


class RemoteProvider:
    name = "remote"

    def __init__(
        self,
        *,
        api_key: str | Callable[[], Any] | None = None,
        credentials: Any = None,
        transport: HTTPTransport | None = None,
        catalog: ModelCatalog | None = None,
    ) -> None:
        if api_key is not None and credentials is not None:
            raise ConfigurationError("Pass api_key or credentials")
        self.api_key = api_key
        self.credentials = credentials
        self.transport = transport or HTTPTransport()
        self.catalog = catalog if catalog is not None else ModelCatalog.bundled()

    def model_info(self, request: ModelRequest) -> ModelInfo:
        """The request's model record, or the catalog's; an unknown model is an error."""
        model = request.model_info or self.catalog.get(self.name, request.model)
        if model is None:
            raise ConfigurationError(
                f"Unknown model {self.name}/{request.model}: pass a ModelInfo as the model "
                "or register one in the provider catalog"
            )
        if model.provider != self.name or model.id != request.model:
            raise ConfigurationError(f"Model {model.provider}/{model.id} is not {self.name}")
        model.validate_request(request)
        return deepcopy(model)

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def credential(self, request: ModelRequest, cancel: CancelToken) -> str:
        key = request.api_key
        if key is None and self.credentials is not None:
            credential = (
                await self.credentials.get(cancel)
                if hasattr(self.credentials, "get")
                else self.credentials
            )
            expected = "openai-chatgpt" if self.name == "openai" else self.name
            if credential.provider != expected:
                raise ConfigurationError("OAuth credential belongs to a different provider")
            if (
                expected == "openai-chatgpt"
                and "chatgpt.tokens.use.direct" not in credential.scopes
            ):
                raise ConfigurationError("OAuth grant lacks chatgpt.tokens.use.direct")
            if credential.expires_at <= time.time():
                raise ConfigurationError("OAuth credential expired; use RefreshingCredentials")
            key = credential.access_token
        if key is None:
            key = await invoke(self.api_key) if callable(self.api_key) else self.api_key
        if not isinstance(key, str) or not key:
            raise ConfigurationError(f"Missing credentials for {self.name}")
        return key

    async def payload(self, request: ModelRequest, body: dict[str, Any]) -> dict[str, Any]:
        payload = deepcopy(body)
        replacement = await invoke(request.on_payload, payload)
        if replacement is not None:
            payload = replacement
        if not isinstance(payload, dict):
            raise ConfigurationError("on_payload must return a dict or None")
        return payload


def normalize_usage(raw: dict[str, Any], provider: str) -> dict[str, int]:
    """Pi token accounting in snake_case. Prices are not inferred from model names."""
    if provider == "anthropic":
        read = raw.get("cache_read_input_tokens", 0)
        write = raw.get("cache_creation_input_tokens", 0)
        input_tokens = raw.get("input_tokens", 0)
        reasoning = 0
    else:
        details = raw.get("input_tokens_details") or {}
        read = details.get("cached_tokens", 0)
        write = details.get("cache_write_tokens", 0)
        input_tokens = max(0, raw.get("input_tokens", 0) - read - write)
        reasoning = (raw.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
    output = raw.get("output_tokens", 0)
    return {
        "input": input_tokens,
        "output": output,
        "cache_read": read,
        "cache_write": write,
        "reasoning": reasoning,
        "total_tokens": input_tokens + output + read + write,
    }
