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
SAMPLING_DELTA_EXTENSION = "io.pi-python/sampling-delta-v1"
MCP_FEATURES = frozenset(
    {
        "sampling-host-state-v1",
        "sampling-profiles-v1",
        "sampling-metering-v1",
        "sampling-retry-v1",
        "sampling-delta-v1",
    }
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


def _json_equal(left: Any, right: Any) -> bool:
    """Conservative wire equality: unlike Python ==, bool is not int (nor float)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return bool(left == right)


def visible_message(message: AssistantMessage) -> AssistantMessage:
    """Projection only: the unmodified original MUST remain in the host state store."""
    if message.deferred is not None:
        raise UnsupportedCapabilityError("Sampling does not support deferred responses")
    # Build just the wire projection. Never clone private thinking/diagnostics
    # merely to throw them away. Mutable tool arguments still get their own copy.
    blocks: list[TextContent | ThinkingContent | ToolCall] = []
    for block in message.content:
        if isinstance(block, ThinkingContent):
            continue
        if isinstance(block, TextContent):
            blocks.append(TextContent(block.text))
        elif isinstance(block, ToolCall):
            if block.namespace is not None:
                raise UnsupportedCapabilityError("Sampling does not support namespaced tools")
            blocks.append(ToolCall(block.id, block.name, deepcopy(block.arguments)))
        else:
            raise UnsupportedCapabilityError("Unsupported sampling response content")
    return AssistantMessage(
        blocks or [TextContent("")],
        stop_reason=message.stop_reason,
        provider=message.provider,
        model=message.model,
        error=message.error,
    )


class _SamplingState:
    """Owned by one outer tool call, never by a process or shared Provider."""

    def __init__(self) -> None:
        self.responses: dict[
            str, tuple[str, str, AssistantMessage, list[TextContent | ThinkingContent | ToolCall]]
        ] = {}
        # These are owned wire projections, not ModelRequests. Prefix blocks and
        # static fields may be shared internally; from_sampling copies mutable
        # fields before any policy hook or Provider sees them.
        self.requests: dict[str, tuple[str, str, dict[str, Any]]] = {}

    def save(
        self,
        conversation: str,
        profile: str,
        message: AssistantMessage,
        visible: AssistantMessage | None = None,
    ) -> str:
        ref = uuid4().hex
        self.responses[ref] = (
            conversation,
            profile,
            deepcopy(message),
            deepcopy((visible if visible is not None else visible_message(message)).content),
        )
        return ref

    def restore(
        self, ref: str, conversation: str, profile: str, visible: AssistantMessage
    ) -> AssistantMessage:
        stored = self.responses.get(ref)
        if stored is None or stored[:2] != (conversation, profile):
            raise UnsupportedCapabilityError("Expired or foreign sampling state reference")
        original = stored[2]
        if not _json_equal([vars(b) for b in stored[3]], [vars(b) for b in visible.content]):
            raise UnsupportedCapabilityError("Sampling history changed a retained response")
        return deepcopy(original)

    def expand_request(
        self, data: dict[str, Any], delta: Any, conversation: str, profile: str
    ) -> dict[str, Any]:
        if not isinstance(delta, dict):
            raise UnsupportedCapabilityError("Invalid sampling delta")
        if not delta:  # Prime a base with a complete request.
            return data
        if set(delta) != {"base", "prefix", "reuse"} or not isinstance(delta["base"], str):
            raise UnsupportedCapabilityError("Invalid sampling delta")
        stored = self.requests.get(delta["base"])
        if stored is None or stored[:2] != (conversation, profile):
            raise UnsupportedCapabilityError("Expired or foreign sampling delta reference")
        base = stored[2]
        prefix, reuse = delta["prefix"], delta["reuse"]
        if type(prefix) is not int or not 0 <= prefix <= len(base["messages"]):
            raise UnsupportedCapabilityError("Invalid sampling delta prefix")
        if (
            not isinstance(reuse, list)
            or any(
                not isinstance(key, str) or key not in {"systemPrompt", "tools"} for key in reuse
            )
            or len(set(reuse)) != len(reuse)
        ):
            raise UnsupportedCapabilityError("Invalid sampling delta fields")
        expanded = {**data, "messages": base["messages"][:prefix] + data["messages"]}
        for key in reuse:
            if key in data or key not in base:
                raise UnsupportedCapabilityError("Conflicting sampling delta field")
            expanded[key] = base[key]
        return expanded

    def save_request(self, ref: str, conversation: str, profile: str, data: dict[str, Any]) -> None:
        # Take ownership of internal data from params.model_dump / expand_request.
        # No caller/Provider has references to this projection's mutable values.
        self.requests[ref] = (
            conversation,
            profile,
            {key: data[key] for key in ("messages", "systemPrompt", "tools") if key in data},
        )

    def clear(self) -> None:
        self.responses.clear()
        self.requests.clear()
