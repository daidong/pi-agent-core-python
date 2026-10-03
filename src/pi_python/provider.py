from __future__ import annotations
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from .cancellation import CancelToken
from .models import ModelInfo
from .messages import (
    AssistantMessage,
    Message,
    ToolDeclaration,
    TextContent,
    ThinkingContent,
    ToolCall,
)
from .errors import ConfigurationError


@dataclass
class ModelRequest:
    messages: list[Message]
    tools: list[ToolDeclaration] = field(default_factory=list)
    model: str = "mock"
    options: dict[str, Any] = field(default_factory=dict)
    # The caller's model record, when given; otherwise providers look `model` up.
    model_info: ModelInfo | None = None
    api_key: str | None = field(default=None, repr=False)
    on_payload: Callable | None = field(default=None, repr=False)
    on_response: Callable | None = field(default=None, repr=False)
    on_provider_stream_event: Callable | None = field(default=None, repr=False)


@dataclass
class ModelEvent:
    """One event of Pi's assistant stream.

    Order: an optional `start`, then for each content block `*_start`, any number of
    `*_delta`, and `*_end` (block kinds: text, thinking, toolcall), then exactly one
    `done` carrying the complete message. Streaming is optional: a provider may yield
    only `done`. Failures are raised; consumers see them as an `error` event.
    `index` is the block's position in the final message.
    """

    type: str
    delta: str = ""
    call_id: str | None = None
    name: str | None = None
    index: int = 0
    message: AssistantMessage | None = None
    partial: AssistantMessage | None = None
    block: TextContent | ThinkingContent | ToolCall | None = None
    content: str | None = None
    reason: str | None = None

    @classmethod
    def text(cls, delta: str, index: int = 0) -> ModelEvent:
        return cls("text_delta", delta, index=index)

    @classmethod
    def thinking(cls, delta: str, index: int = 0) -> ModelEvent:
        return cls("thinking_delta", delta, index=index)

    @classmethod
    def toolcall(cls, delta: str, index: int = 0) -> ModelEvent:
        """A fragment of the JSON arguments of the tool call block at `index`."""
        return cls("toolcall_delta", delta, index=index)

    @classmethod
    def boundary(
        cls, phase: str, index: int, block: TextContent | ThinkingContent | ToolCall
    ) -> ModelEvent:
        """`phase` is "start" or "end"; the end event carries the complete block."""
        kind = "toolcall" if isinstance(block, ToolCall) else block.type
        return cls(
            f"{kind}_{phase}",
            index=index,
            block=block,
            call_id=block.id if isinstance(block, ToolCall) else None,
            name=block.name if isinstance(block, ToolCall) else None,
        )

    @classmethod
    def done(cls, message: AssistantMessage) -> ModelEvent:
        return cls("done", message=message, reason=message.stop_reason)


class Provider(Protocol):
    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]: ...


_default_stream: Provider | Callable | None = None


def set_default_stream_fn(stream: Provider | Callable | None) -> None:
    """Explicit process-wide fallback, matching Pi's setDefaultStreamFn."""
    global _default_stream
    _default_stream = stream


class FunctionProvider:
    def __init__(self, function: Callable):
        self.function = function

    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        return self.function(request, cancel)


def has_default_stream() -> bool:
    return _default_stream is not None


class DefaultProvider:
    @property
    def name(self) -> str:
        return getattr(_default_stream, "name", "custom")

    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        if _default_stream is None:
            raise ConfigurationError(
                "No default stream configured; pass provider or call set_default_stream_fn"
            )
        if hasattr(_default_stream, "stream"):
            return _default_stream.stream(request, cancel)
        return _default_stream(request, cancel)
