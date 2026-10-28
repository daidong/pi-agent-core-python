"""Pi-shaped contracts over the shared execution engine.

Names use snake_case; messages retain Python's documented seconds/enum mapping.
The root Agent is the research convenience API. This module has no automatic
cache session, failed-assistant retry, or implicit result-string hook protocol.
"""

from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Callable
import inspect

from .agent import Agent as ResearchAgent
from .cancellation import CancelToken
from .errors import ConfigurationError, InvalidContinuationError
from .hooks import Hooks as RuntimeHooks, RunContext, TurnUpdate as RuntimeUpdate
from .messages import AssistantMessage, Message, ToolResultMessage, _blocks, message_to_dict
from .models import ModelInfo
from .provider import ModelRequest
from .tools import Tool as RuntimeTool, ToolContext, ToolResult, invoke


@dataclass
class AgentContext:
    messages: list[Message] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)


@dataclass
class PrepareRequestContext:
    context: AgentContext
    model: str | ModelInfo
    thinking_level: str


@dataclass
class AgentTurnContext:
    message: AssistantMessage | None
    tool_results: list[ToolResultMessage]
    context: AgentContext
    new_messages: list[Message]


@dataclass
class AgentRequestUpdate:
    context: AgentContext | None = None
    model: str | ModelInfo | None = None
    thinking_level: str | None = None


@dataclass
class AgentLoopTurnUpdate(AgentRequestUpdate):
    messages: list[Message] = field(default_factory=list)


@dataclass
class BeforeToolCallContext:
    assistant_message: AssistantMessage | None
    tool_call: Any
    args: Any
    context: AgentContext


@dataclass
class AfterToolCallContext(BeforeToolCallContext):
    result: ToolResult
    is_error: bool


def _context(value: RunContext) -> AgentContext:
    return AgentContext(
        deepcopy(value.messages), deepcopy([getattr(t, "_core_tool", t) for t in value.tools])
    )


def _turn(value: RunContext) -> AgentTurnContext:
    return AgentTurnContext(value.message, value.tool_results, _context(value), value.new_messages)


def _result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if not isinstance(value, dict):
        raise ConfigurationError("Tool result must be ToolResult or a result object")
    data = deepcopy(value)
    if data["content"] and isinstance(data["content"][0], dict):
        data["content"] = _blocks(data["content"], images=True)
    return ToolResult(**data)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: Callable
    label: str = ""
    output_schema: dict[str, Any] | None = None
    execution_mode: str = "parallel"
    prepare_arguments: Callable | None = None

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.name

    def __deepcopy__(self, memo: dict[int, Any]) -> Tool:
        result = copy(self)
        memo[id(self)] = result
        result.parameters = deepcopy(self.parameters, memo)
        result.output_schema = deepcopy(self.output_schema, memo)
        return result

    def to_runtime(self) -> RuntimeTool:
        async def execute(args: Any, context: ToolContext) -> ToolResult:
            active = True

            async def update(value: Any) -> None:
                if active:
                    await context.emit_update(asdict(_result(value)))

            try:
                return _result(
                    await invoke(self.execute, context.call_id, args, context.cancel, update)
                )
            finally:
                active = False

        tool = RuntimeTool(
            self.name,
            self.description,
            self.parameters,
            execute,
            self.output_schema,
            self.execution_mode,
            self.prepare_arguments,
        )
        setattr(tool, "_core_tool", self)
        return tool


def _tools(values: list[Any]) -> list[RuntimeTool]:
    return [v.to_runtime() if isinstance(v, Tool) else v for v in values]


def _stream_fn(function: Callable) -> Callable:
    async def stream(request: ModelRequest, signal: CancelToken) -> Any:
        options = {**deepcopy(request.options), "signal": signal}
        for name in ("api_key", "on_payload", "on_response", "on_provider_stream_event"):
            value = getattr(request, name)
            if value is not None:
                options[name] = value
        source = function(
            request.model_info or request.model,
            AgentContext(deepcopy(request.messages), deepcopy(request.tools)),
            options,
        )
        if inspect.isawaitable(source):
            source = await source
        try:
            async for event in source:
                yield event
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()

    return stream


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

    @classmethod
    def from_options(cls, options: dict[str, Any], hooks: Hooks | None = None) -> Hooks:
        overrides = {f.name: options.pop(f.name) for f in fields(cls) if f.name in options}
        return replace(hooks or cls(), **overrides)

    def to_runtime(self) -> RuntimeHooks:
        async def prepare(
            name: str, value: RunContext, signal: CancelToken
        ) -> RuntimeUpdate | None:
            incoming = (
                PrepareRequestContext(
                    _context(value), value.model, value.options.get("reasoning", "off")
                )
                if name == "prepare_request"
                else _turn(value)
            )
            update = await invoke(getattr(self, name), incoming, signal)
            if update is None:
                return None
            expected = AgentRequestUpdate if name == "prepare_request" else AgentLoopTurnUpdate
            if isinstance(update, dict):
                data = dict(update)
                if isinstance(data.get("context"), dict):
                    data["context"] = AgentContext(**data["context"])
                update = expected(**data)
            if not isinstance(update, expected) or (
                name == "prepare_request" and isinstance(update, AgentLoopTurnUpdate)
            ):
                raise ConfigurationError(f"{name} must return {expected.__name__}")
            options = None
            if update.thinking_level is not None:
                options = {**value.options, "reasoning": update.thinking_level}
            return RuntimeUpdate(
                tools=_tools(update.context.tools) if update.context is not None else None,
                model=update.model,
                options=options,
                context=update.context.messages if update.context is not None else None,
                messages=update.messages if isinstance(update, AgentLoopTurnUpdate) else [],
            )

        async def before(call: Any, args: Any, ctx: ToolContext) -> ToolResult | None:
            decision = await invoke(
                self.before_tool_call,
                BeforeToolCallContext(
                    ctx.assistant_message, call, args, _context(ctx.agent_context)
                ),
                ctx.cancel,
            )
            if decision is None:
                return None
            if not isinstance(decision, dict) or set(decision) - {"block", "reason", "terminate"}:
                raise ConfigurationError(
                    "before_tool_call must return a block/reason/terminate object"
                )
            if decision.get("block"):
                return ToolResult.text(
                    decision.get("reason") or "Tool call blocked",
                    is_error=True,
                    terminate=decision.get("terminate", False),
                )
            return None

        async def after(call: Any, result: ToolResult, ctx: ToolContext) -> ToolResult:
            update = await invoke(
                self.after_tool_call,
                AfterToolCallContext(
                    ctx.assistant_message,
                    call,
                    ctx.args,
                    _context(ctx.agent_context),
                    result,
                    result.is_error,
                ),
                ctx.cancel,
            )
            if update is None:
                return result
            allowed = {"content", "details", "structured_content", "is_error", "usage", "terminate"}
            if not isinstance(update, dict) or set(update) - allowed:
                raise ConfigurationError("after_tool_call must return a partial result object")
            if "content" in update and "structured_content" not in update:
                result.structured_content = None
            for key, value in update.items():
                if key == "content":
                    value = (
                        _blocks(value, images=True)
                        if value and isinstance(value[0], dict)
                        else value
                    )
                setattr(result, key, deepcopy(value))
            return result

        async def finish(value: RunContext, signal: CancelToken) -> str | None:
            decision = await invoke(self.finish_turn, _turn(value), signal)
            if decision is None:
                return None
            if (
                not isinstance(decision, dict)
                or set(decision) != {"action"}
                or decision["action"] not in {"end", "continue"}
            ):
                raise ConfigurationError("finish_turn must return {action: end|continue}")
            return decision["action"]

        return RuntimeHooks(
            prepare_request=lambda ctx, signal: prepare("prepare_request", ctx, signal),
            prepare_next_turn=lambda ctx, signal: prepare("prepare_next_turn", ctx, signal),
            before_tool_call=before if self.before_tool_call else None,
            after_tool_call=after if self.after_tool_call else None,
            finish_turn=finish if self.finish_turn else None,
            **{
                name: getattr(self, name)
                for name in (
                    "transform_context",
                    "convert_to_llm",
                    "get_api_key",
                    "on_payload",
                    "on_response",
                    "on_provider_stream_event",
                )
            },
        )


def event_payload(run: Any, kind: str, call_id: str | None, data: dict[str, Any]) -> dict[str, Any]:
    """Canonical payload produced at the source; never reconstruct from UI state."""
    if kind == "agent_end":
        return {"messages": [message_to_dict(m) for m in run.context.new_messages]}
    if kind == "message_update":
        return {
            "message": data["partial"],
            "assistant_message_event": data["assistant_message_event"],
        }
    if kind.startswith("tool_execution_"):
        outcome = next(o for o in run.active_batch if o.call.id == call_id)
        payload = {"tool_call_id": call_id, "tool_name": outcome.call.name}
        if kind == "tool_execution_end":
            payload.update(result=data["result"], is_error=outcome.result.is_error)
            if "duration_ms" in data:
                payload["duration_ms"] = data["duration_ms"]
        else:
            payload["args"] = deepcopy(outcome.original_arguments)
            if kind == "tool_execution_update":
                payload["partial_result"] = data["update"]
        return payload
    return data


@dataclass(frozen=True)
class AgentState:
    messages: list[Message]
    model: str | ModelInfo
    thinking_level: str
    tools: list[Any]
    system_prompt: str
    is_streaming: bool
    streaming_message: Any
    pending_tool_calls: frozenset[str]
    error_message: str | None


class Agent:
    """Pi-shaped session API. Research convenience methods remain on root Agent."""

    def __init__(
        self, *, hooks: Hooks | None = None, tools: list[Any] | None = None, **options: Any
    ):
        if "auto_session_id" in options or "cache_session_id" in options:
            raise ConfigurationError("Automatic cache sessions belong to pi_python.Agent")
        if options.get("stream_fn") is not None:
            options["stream_fn"] = _stream_fn(options["stream_fn"])
        next_with_context = options.pop("prepare_next_turn_with_context", None)
        if next_with_context is not None:
            options["prepare_next_turn"] = next_with_context
        elif options.get("prepare_next_turn") is not None:
            next_without_context = options["prepare_next_turn"]
            options["prepare_next_turn"] = lambda context, signal: invoke(
                next_without_context, signal
            )
        self._agent = ResearchAgent(
            hooks=Hooks.from_options(options, hooks).to_runtime(),
            tools=_tools(tools or []),
            auto_session_id=False,
            **options,
        )
        self._agent._core_contract = True

    @property
    def state(self) -> AgentState:
        from .transcript import current_system_prompt

        view = self._agent.state
        config = (
            self._agent._run.context
            if self._agent._run and self._agent._running
            else self._agent._defaults
        )
        messages = list(view.messages)
        return AgentState(
            messages,
            deepcopy(config.model or "mock"),
            (config.options or {}).get("reasoning", "off"),
            deepcopy([getattr(t, "_core_tool", t) for t in config.tools or []]),
            current_system_prompt(messages),
            view.is_running,
            view.partial_response,
            frozenset(view.pending_calls),
            view.last_error,
        )

    @property
    def signal(self) -> CancelToken | None:
        return self._agent.signal

    def subscribe(self, listener: Callable) -> Callable:
        return self._agent.subscribe(
            lambda event: invoke(listener, {"type": event.type, **event.data}, self.signal)
        )

    async def prompt(self, message: Any) -> None:
        await self._agent.prompt(message)

    async def continue_run(self) -> None:
        messages = self._agent.state.messages
        if not messages or (isinstance(messages[-1], AssistantMessage) and not self._agent._queues):
            raise InvalidContinuationError("Cannot continue from empty context or assistant")
        await self._agent.continue_run()

    def steer(self, message: Any) -> None:
        self._agent.steer(message)

    def follow_up(self, message: Any) -> None:
        self._agent.follow_up(message)

    def abort(self) -> None:
        self._agent.abort()

    async def wait_for_idle(self) -> None:
        await self._agent.wait_for_idle()

    def reset(self) -> None:
        self._agent.reset()

    async def aclose(self) -> None:
        await self._agent.aclose()

    async def __aenter__(self) -> Agent:
        await self._agent.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()


@dataclass
class AgentLoopConfig(Hooks):
    model: str | ModelInfo = "mock"
    provider: Any = None
    stream_fn: Callable | None = None
    options: dict[str, Any] = field(default_factory=dict)
    hooks: Hooks | None = None
    thinking_level: str | None = None
    tool_execution: str = "parallel"
    get_steering_messages: Callable | None = None
    get_follow_up_messages: Callable | None = None


async def _run(
    prompts: Any,
    context: AgentContext,
    config: AgentLoopConfig,
    emit: Callable | None,
    cancel: CancelToken | None,
    continuing: bool,
) -> list[Message]:
    from . import lowlevel

    if continuing and (not context.messages or isinstance(context.messages[-1], AssistantMessage)):
        raise InvalidContinuationError("Cannot continue from empty context or assistant")
    options = deepcopy(config.options)
    if config.thinking_level is not None:
        options["reasoning"] = config.thinking_level
    runtime_config = lowlevel.AgentLoopConfig(
        provider=config.provider,
        stream_fn=_stream_fn(config.stream_fn) if config.stream_fn else None,
        model=config.model,
        options=options,
        hooks=Hooks.from_options(
            {
                f.name: getattr(config, f.name)
                for f in fields(Hooks)
                if getattr(config, f.name) is not None
            },
            config.hooks,
        ).to_runtime(),
        tool_execution=config.tool_execution,
        get_steering_messages=config.get_steering_messages,
        get_follow_up_messages=config.get_follow_up_messages,
    )
    setattr(runtime_config, "_core_contract", True)
    runtime_context = lowlevel.AgentContext(context.messages, _tools(context.tools))

    async def forward(event: Any) -> None:
        await invoke(emit, {"type": event.type, **event.data})

    result = await lowlevel._run(
        prompts, runtime_context, runtime_config, forward, cancel, continuing
    )
    if continuing:
        context.messages[:] = runtime_context.messages
    return result


async def run_agent_loop(
    prompts: list[Message],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: Callable | None = None,
    cancel: CancelToken | None = None,
) -> list[Message]:
    return await _run(prompts, context, config, emit, cancel, False)


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: Callable | None = None,
    cancel: CancelToken | None = None,
) -> list[Message]:
    return await _run(None, context, config, emit, cancel, True)


def agent_loop(
    prompts: list[Message],
    context: AgentContext,
    config: AgentLoopConfig,
    cancel: CancelToken | None = None,
) -> Any:
    from .lowlevel import AgentEventStream

    return AgentEventStream(
        lambda emit: run_agent_loop(prompts, context, config, emit, cancel), maxsize=0
    )


def agent_loop_continue(
    context: AgentContext, config: AgentLoopConfig, cancel: CancelToken | None = None
) -> Any:
    from .lowlevel import AgentEventStream

    return AgentEventStream(
        lambda emit: run_agent_loop_continue(context, config, emit, cancel), maxsize=0
    )
