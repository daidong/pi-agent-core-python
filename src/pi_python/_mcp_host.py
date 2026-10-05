"""Host-only sampling policy and scoped vendor replay state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import os
from typing import TYPE_CHECKING, Any, Literal, Mapping
from uuid import uuid4

from .errors import ConfigurationError, UnsupportedCapabilityError
from .messages import AssistantMessage, TextContent, ThinkingContent, ToolCall
from .models import ModelInfo

if TYPE_CHECKING:
    from ._mcp_interaction import MCPRequestContext

# A versioned, explicitly negotiated extension, not an MCP protocol capability.
SAMPLING_EXTENSION = "io.pi-python/sampling-v1"
MCP_FEATURES = frozenset(
    {"sampling-host-state-v1", "sampling-profiles-v1", "sampling-metering-v1", "sampling-retry-v1"}
    | ({"stdio-process-scope-v1"} if os.name == "posix" else set())
)


@dataclass(frozen=True)
class SamplingProfile:
    """A named host-approved model configuration. It never crosses the wire."""

    model: str | ModelInfo
    options: Mapping[str, Any] = field(default_factory=dict)
    tool_choice_format: Literal["openai", "anthropic"] = "openai"


@dataclass(frozen=True)
class SamplingRetryPolicy:
    """Retry model requests only; the callback timeout also bounds all backoff."""

    max_attempts: int = 1
    initial_delay: float = 0.5
    max_delay: float = 30

    def __post_init__(self) -> None:
        import math

        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ConfigurationError("Sampling max_attempts must be positive")
        for value in (self.initial_delay, self.max_delay):
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ConfigurationError("Sampling retry delays must be finite and nonnegative")


@dataclass(frozen=True)
class SamplingObservation:
    """One completed Provider attempt; unknown usage is None, never synthesized zero.

    No prompt, response text, credential or raw exception is included. Observers
    run before retry/conversion and must not raise; an observer failure stops the
    callback without retrying an already completed model response.
    """

    context: MCPRequestContext
    profile: str
    attempt: int
    status: str
    usage: Mapping[str, Any] | None
    response_id: str | None = None
    category: str | None = None
    retry_after: float | None = None


class SamplingFailure(Exception):
    """Sanitized model failure. Retry is owned by the host, not the business tool."""

    def __init__(
        self,
        category: str = "provider",
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(f"Sampling model request failed ({category})")
        self.category, self.retryable = category, retryable
        self.retry_after, self.attempts = retry_after, attempts


def visible_message(message: AssistantMessage) -> AssistantMessage:
    """Projection only: the unmodified original MUST remain in the host state store."""
    if message.deferred is not None:
        raise UnsupportedCapabilityError("Sampling does not support deferred responses")
    result = deepcopy(message)
    blocks: list[TextContent | ThinkingContent | ToolCall] = []
    for block in result.content:
        if isinstance(block, ThinkingContent):
            continue
        if isinstance(block, TextContent):
            block.text_signature = None
        elif isinstance(block, ToolCall):
            if block.namespace is not None:
                raise UnsupportedCapabilityError("Sampling does not support namespaced tools")
            block.thought_signature = None
        else:
            raise UnsupportedCapabilityError("Unsupported sampling response content")
        blocks.append(block)
    result.content = blocks or [TextContent("")]
    return result


class _SamplingState:
    """Owned by one outer tool call, never by a process or shared Provider."""

    def __init__(self) -> None:
        self.responses: dict[str, tuple[str, str, AssistantMessage]] = {}

    def save(self, conversation: str, profile: str, message: AssistantMessage) -> str:
        ref = uuid4().hex
        self.responses[ref] = (conversation, profile, deepcopy(message))
        return ref

    def restore(
        self, ref: str, conversation: str, profile: str, visible: AssistantMessage
    ) -> AssistantMessage:
        stored = self.responses.get(ref)
        if stored is None or stored[:2] != (conversation, profile):
            raise UnsupportedCapabilityError("Expired or foreign sampling state reference")
        original = stored[2]
        if visible_message(original).content != visible.content:
            raise UnsupportedCapabilityError("Sampling history changed a retained response")
        return deepcopy(original)

    def clear(self) -> None:
        self.responses.clear()
