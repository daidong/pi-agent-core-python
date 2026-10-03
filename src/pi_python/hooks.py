from dataclasses import dataclass, field
from typing import Any, Callable
from .messages import AssistantMessage, Message, ToolResultMessage
from .models import ModelInfo
from .tools import Tool


@dataclass
class AgentConfigUpdate:
    tools: list[Tool] | None = None
    model: str | ModelInfo | None = None
    options: dict[str, Any] | None = None


@dataclass
class TurnUpdate(AgentConfigUpdate):
    context: list[Message] | None = None
    messages: list[Message] = field(default_factory=list)


@dataclass
class RunContext:
    messages: list[Message]
    model: str | ModelInfo
    options: dict[str, Any]
    tools: list[Tool]
    message: AssistantMessage | None = None
    tool_results: list[ToolResultMessage] = field(default_factory=list)
    new_messages: list[Message] = field(default_factory=list)


@dataclass
class Hooks:
    prepare_request: Callable | None = None
    prepare_next_turn: Callable | None = None
    transform_context: Callable | None = None
    convert_to_llm: Callable | None = None
    before_tool_call: Callable | None = None
    after_tool_call: Callable | None = None
    finish_turn: Callable | None = None
    get_api_key: Callable | None = None
    on_payload: Callable | None = None
    on_response: Callable | None = None
    on_provider_stream_event: Callable | None = None
