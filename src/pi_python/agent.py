"""The Agent session: history, default configuration, queues and subscribers.

Each prompt or continuation executes as a Run (run.py) through the loop (loop.py).
Execution behavior is adapted from Pi; see NOTICE.
"""

from __future__ import annotations
import asyncio
from copy import copy, deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from .cancellation import CancelToken
from .errors import (
    AgentBusyError,
    AgentClosedError,
    CleanupTimeoutError,
    ConfigurationError,
    InvalidContinuationError,
    ToolOutcomeUnknownError,
)
from .events import EventDispatcher, EventListener, Unsubscribe
from .hooks import AgentConfigUpdate, Hooks
from .limits import RunLimits
from .models import ModelInfo
from .messages import (
    AssistantMessage,
    ImageContent,
    TextContent,
    Message,
    SystemMessage,
    UserMessage,
    validate_history,
    validate_json,
)
from .provider import Provider, DefaultProvider, FunctionProvider, has_default_stream
from .queues import MessageQueues
from .run import CALLER_CANCELLED, Run, RunResult, merge
from .sync import run_sync
from .tools import Tool
from .transcript import current_system_message


@dataclass(frozen=True)
class AgentStateView:
    messages: tuple[Message, ...]
    is_running: bool
    partial_response: Any
    pending_calls: tuple[str, ...]
    last_error: str | None
    reconciliation_required: bool
    cleanup_complete: bool
    closed: bool
    diagnostics: tuple[dict[str, Any], ...]


class Agent:
    def __init__(
        self,
        *,
        provider: Provider | None = None,
        stream_fn: Callable | None = None,
        model: str | ModelInfo = "mock",
        options: dict[str, Any] | None = None,
        system_prompt: str = "",
        thinking_level: str | None = None,
        thinking_budgets: dict[str, int] | None = None,
        transport: str | None = None,
        session_id: str | None = None,
        get_api_key: Callable | None = None,
        on_payload: Callable | None = None,
        on_response: Callable | None = None,
        on_provider_stream_event: Callable | None = None,
        tools: list[Tool] | None = None,
        messages: list[Message] | None = None,
        hooks: Hooks | None = None,
        limits: RunLimits | None = None,
        execution_mode: str = "parallel",
        steering_mode: str = "one_at_a_time",
        follow_up_mode: str = "one_at_a_time",
    ):
        if execution_mode not in {"parallel", "sequential"}:
            raise ConfigurationError("Invalid execution mode")
        self._queues = MessageQueues(steering_mode, follow_up_mode)
        if provider is not None and stream_fn is not None:
            raise ConfigurationError("Pass provider or stream_fn, not both")
        self.provider = provider or (
            FunctionProvider(stream_fn) if stream_fn else DefaultProvider()
        )
        self.hooks = copy(hooks) if hooks else Hooks()
        for name, callback in (
            ("get_api_key", get_api_key),
            ("on_payload", on_payload),
            ("on_response", on_response),
            ("on_provider_stream_event", on_provider_stream_event),
        ):
            if callback is not None:
                setattr(self.hooks, name, callback)
        options = deepcopy(options or {})
        for name, value in (
            ("reasoning", thinking_level),
            ("thinking_budgets", thinking_budgets),
            ("transport", transport),
            ("session_id", session_id),
        ):
            if value is not None:
                options[name] = value
        self.limits = limits or RunLimits()
        self.execution_mode = execution_mode
        self._get_steering_messages: Callable | None = None
        self._get_follow_up_messages: Callable | None = None
        self._defaults = AgentConfigUpdate(tools or [], model, options or {})
        self._validate_update(self._defaults)
        self._defaults = deepcopy(self._defaults)
        self._messages = deepcopy(messages or [])
        validate_history(self._messages)
        if (system_prompt or tools) and not (
            self._messages and isinstance(self._messages[0], SystemMessage)
        ):
            self._messages.insert(
                0,
                SystemMessage(
                    system_prompt, tools_added=[t.declaration() for t in tools or []], timestamp=0
                ),
            )
        validate_history(self._messages)
        self._updates: list[AgentConfigUpdate] = []
        self._events = EventDispatcher()
        self._run: Run | None = None
        self._running = False
        self._closed = False
        # These outlive a run: an outcome a tool explicitly reported as unknown, or an
        # unfinished cleanup, makes the instance unusable until the application
        # reconciles or discards it. Cancelling a tool never does.
        self._unknown = False
        self._cleanup_complete = True
        self._last_error: str | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        # Unlike _idle (which also wakes callers on a cleanup timeout), this
        # marks actual completion of the run and all operations it owns.
        self._settled = asyncio.Event()
        self._settled.set()

    @property
    def steering_mode(self) -> str:
        return self._queues.steering_mode

    @property
    def follow_up_mode(self) -> str:
        return self._queues.follow_up_mode

    @property
    def state(self) -> AgentStateView:
        run = self._run if self._running else None
        return AgentStateView(
            tuple(deepcopy(self._messages)),
            self._running,
            deepcopy(run.partial) if run else None,
            run.pending_calls if run else (),
            self._last_error,
            self._unknown,
            self._cleanup_complete,
            self._closed,
            tuple(deepcopy(self._events.diagnostics)),
        )

    def subscribe(self, listener: EventListener) -> Unsubscribe:
        return self._events.subscribe(listener)

    def _usable(self) -> None:
        if self._closed:
            raise AgentClosedError("Agent is closed")
        if not self._cleanup_complete:
            raise CleanupTimeoutError(
                "Managed operations did not finish cleanup; discard this Agent"
            )
        if self._unknown:
            raise ToolOutcomeUnknownError("External outcome unknown; reconcile outside this Agent")

    def _validate_update(self, update: AgentConfigUpdate) -> None:
        if not isinstance(update, AgentConfigUpdate):
            raise ConfigurationError("Expected AgentConfigUpdate")
        if update.model is not None and not isinstance(update.model, (str, ModelInfo)):
            raise ConfigurationError("model must be a model name or a ModelInfo")
        if update.options is not None:
            validate_json(update.options)
            if not isinstance(update.options, dict):
                raise ConfigurationError("options must be an object")
        if update.tools is not None:
            names = []
            for tool in update.tools:
                if not isinstance(tool, Tool):
                    raise ConfigurationError("Expected Tool")
                tool.__post_init__()
                names.append(tool.name)
            if len(set(names)) != len(names):
                raise ConfigurationError("Duplicate tool names")

    def update_config(self, update: AgentConfigUpdate) -> None:
        self._usable()
        self._validate_update(update)
        update = deepcopy(update)
        if self._running:
            self._updates.append(update)
        else:
            merge(self._defaults, update)

    @staticmethod
    def _input(message: str | Message | list[Message]) -> list[Message]:
        result: list[Message] = (
            [UserMessage(message)]
            if isinstance(message, str)
            else (message if isinstance(message, list) else [message])
        )
        if not result:
            raise InvalidContinuationError("Empty prompt")
        result = deepcopy(result)
        validate_history(result)
        return result

    def steer(self, message: str | Message) -> None:
        self._usable()
        self._queues.steering.extend(self._input(message))

    def follow_up(self, message: str | Message) -> None:
        self._usable()
        self._queues.follow_up.extend(self._input(message))

    def clear_queues(self, *, steering: bool, follow_up: bool) -> None:
        self._queues.clear(steering=steering, follow_up=follow_up)

    def has_queued_messages(self) -> bool:
        return bool(self._queues)

    def peek_queued_messages(self) -> list[Message]:
        return self._queues.peek()

    def clear_steering_queue(self) -> None:
        self.clear_queues(steering=True, follow_up=False)

    def clear_follow_up_queue(self) -> None:
        self.clear_queues(steering=False, follow_up=True)

    def clear_all_queues(self) -> None:
        self.clear_queues(steering=True, follow_up=True)

    @property
    def signal(self) -> CancelToken | None:
        return self._run.token if self._running and self._run else None

    def reset(self) -> None:
        self._usable()
        if self._running:
            raise AgentBusyError("Cannot reset while running")
        baseline = current_system_message(self._messages)
        self._messages = [baseline] if baseline else []
        self._last_error = None
        self._run = None
        self.clear_all_queues()

    def abort(self, reason: str = "requested") -> None:
        """Signal the run, as Pi does. Running operations receive task cancellation and
        settle within the cleanup deadline; the run then ends with an aborted response.

        Safe to call from any thread, for example a GUI or a watchdog.
        """
        run = self._run
        if not (self._running and run) or run.token.cancelled:
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if run.loop is not None and current is not run.loop:
            run.loop.call_soon_threadsafe(self._abort_on_loop, run, reason)
        else:
            self._abort_on_loop(run, reason)

    def _abort_on_loop(self, run: Run, reason: str) -> None:
        if self._run is run and self._running and not run.token.cancelled:
            run.token.cancel(reason)

    def _cleanup_finished(self) -> None:
        """Operations left running by a cleanup timeout have all ended."""
        self._cleanup_complete = True
        run = self._run
        if run is None or run.driver is None or run.driver.done():
            self._running = False
            self._idle.set()
            self._settled.set()

    async def _aclose_owned(self) -> None:
        """Join an exclusively owned child, even beyond its cleanup deadline.

        The enclosing tool remains pending, so its parent Run can enforce its
        own deadline without losing ownership of unfinished descendant work.
        """
        try:
            await self.aclose()
        except CleanupTimeoutError:
            await self._settled.wait()

    async def wait_for_idle(self) -> None:
        if not self._cleanup_complete:
            raise CleanupTimeoutError("Cleanup deadline exceeded")
        await self._idle.wait()
        if not self._cleanup_complete:
            raise CleanupTimeoutError("Cleanup deadline exceeded")

    async def aclose(self) -> None:
        self._closed = True
        self.abort("closed")
        await self.wait_for_idle()

    async def __aenter__(self) -> Agent:
        self._usable()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def prompt(
        self, message: str | Message | list[Message], images: list[ImageContent] | None = None
    ) -> RunResult:
        if images is not None:
            if not isinstance(message, str):
                raise ConfigurationError("images requires a string prompt")
            message = UserMessage([TextContent(message), *images])
        self._usable()
        if self._running:
            raise AgentBusyError("Agent already running")
        return await self._start(self._input(message))

    def prompt_sync(
        self, message: str | Message | list[Message], images: list[ImageContent] | None = None
    ) -> RunResult:
        """Blocking `prompt` for plain scripts; Ctrl+C aborts the run.

        Runs on a shared background event loop, so use one style per Agent: either
        these blocking calls or the async API inside your own event loop.
        """
        return run_sync(self.prompt(message, images), on_interrupt=self.abort)

    def continue_run_sync(self) -> RunResult:
        """Blocking `continue_run`; see `prompt_sync`."""
        return run_sync(self.continue_run(), on_interrupt=self.abort)

    async def continue_run(self) -> RunResult:
        self._usable()
        if self._running:
            raise AgentBusyError("Agent already running")
        validate_history(self._messages)
        queues = self._queues
        tail = self._messages[-1] if self._messages else None
        # A failed or aborted last response can be retried: Provider replay skips it.
        # (Pi's coding agent deletes it first; here it stays in the record.)
        retry = isinstance(tail, AssistantMessage) and tail.stop_reason in {"error", "aborted"}
        if (
            tail is None
            or all(isinstance(m, SystemMessage) for m in self._messages)
            or (isinstance(tail, AssistantMessage) and not queues and not retry)
        ):
            raise InvalidContinuationError("No unfinished interaction or queued messages")
        if isinstance(tail, AssistantMessage) and queues:
            if queues.steering:
                return await self._start(queues.take(True), skip_initial_steering=True)
            return await self._start(queues.take(False))
        return await self._start([])

    async def _start(
        self, pending: list[Message], skip_initial_steering: bool = False
    ) -> RunResult:
        if isinstance(self.provider, DefaultProvider) and not has_default_stream():
            raise ConfigurationError(
                "No model provider: pass Agent(provider=...) or call set_default_stream_fn(...)"
            )
        self._running = True
        self._idle.clear()
        self._settled.clear()
        self._last_error = None
        self._events.failed = False
        run = self._run = Run(self, skip_initial_steering)
        run.loop = asyncio.get_running_loop()
        # Driver starts with a checkpoint so cancellation before scheduling still finalizes.
        run.driver = asyncio.create_task(self._drive(run, pending))
        timer = None
        if self.limits.run_timeout is not None:
            timer = asyncio.get_running_loop().call_later(
                self.limits.run_timeout, self.abort, "run_timeout"
            )
        try:
            return await asyncio.shield(run.driver)
        except asyncio.CancelledError:
            run.token.cancel(CALLER_CANCELLED)
            if run.driver_started and not run.finalizing and not run.driver.done():
                run.driver.cancel()
            # A second caller cancellation must not orphan the cleanup owner.
            while not run.driver.done():
                try:
                    await asyncio.shield(run.driver)
                except asyncio.CancelledError:
                    continue
            raise
        finally:
            if timer is not None:
                timer.cancel()

    async def _drive(self, run: Run, pending: list[Message]) -> RunResult:
        result = await run.drive(pending)
        self._running = not self._cleanup_complete
        self._idle.set()  # Wakes waiters; they check cleanup_complete before returning.
        if self._cleanup_complete:
            self._settled.set()
        return result
