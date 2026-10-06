"""One prompt or continuation: its tasks, cancellation, tool outcomes and events."""

from __future__ import annotations
from .limits import RunLimits
from .provider import Provider
import asyncio
from collections.abc import Awaitable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from .cancellation import CancelToken
from .errors import ConfigurationError
from .hooks import AgentConfigUpdate, Hooks, RunContext, TurnUpdate
from .messages import (
    AssistantMessage,
    Message,
    ToolResultMessage,
    message_to_dict,
    validate_history,
)
from .errors import SubscriptionError
from .tools import ToolOutcome, error_result, invoke
from .transcript import declare_tool_changes
from .loop import abort_error, failure_message, run_loop

if TYPE_CHECKING:
    from .agent import Agent

# Token reason for cancellation of the caller's Python task. Unlike an explicit abort,
# it unwinds immediately and re-raises CancelledError to the caller.
CALLER_CANCELLED = "caller_cancelled"


@dataclass
class RunResult:
    status: str
    messages: list[Message]
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    reconciliation_required: bool = False
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)
    cleanup_complete: bool = True
    queued_steering: int = 0
    queued_follow_up: int = 0


def merge(target: Any, update: AgentConfigUpdate) -> None:
    """Field-wise replacement; mutable values are copied, never deep-merged."""
    for key in ("model", "options", "tools"):
        value = getattr(update, key)
        if value is not None:
            setattr(target, key, deepcopy(value))


class Run:
    """State that lives for one run. The Agent keeps everything that outlives it."""

    def __init__(self, agent: Agent, skip_initial_steering: bool = False):
        self.agent = agent
        self.token = CancelToken()
        self.id = str(uuid4())
        self.turn = 0
        defaults = agent._defaults
        self.context = RunContext(
            deepcopy(agent._messages),
            defaults.model or "mock",
            deepcopy(defaults.options or {}),
            deepcopy(defaults.tools or []),
        )
        self.start = len(agent._messages)
        self.skip_initial_steering = skip_initial_steering
        self.partial: Any = None
        self.started = False  # message_start was published for the current response
        self.outcomes: list[ToolOutcome] = []
        self.active_batch: list[ToolOutcome] = []
        self._committed: set[int] = set()
        self._ended: set[int] = set()
        self._owned: set[asyncio.Future] = set()
        self.driver: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.driver_started = False
        self.finalizing = False

    # Agent configuration seen by the loop.
    @property
    def provider(self) -> Provider:
        return self.agent.provider

    @property
    def hooks(self) -> Hooks:
        return self.agent.hooks

    @property
    def limits(self) -> RunLimits:
        return self.agent.limits

    @property
    def execution_mode(self) -> str:
        return self.agent.execution_mode

    @property
    def pending_calls(self) -> tuple[str, ...]:
        return tuple(o.call.id for o in self.active_batch if id(o) not in self._committed)

    def fail(self, error: str) -> None:
        self.agent._last_error = error

    def require_reconciliation(self) -> None:
        """A descendant's unknown external outcome also makes this Agent unsafe to reuse."""
        self.agent._unknown = True
        # Wake the owner now: a parallel sibling may still be joining an
        # uncooperative descendant. Its cleanup is bounded by this Run too.
        self.token.cancel("outcome_unknown")

    def aborted(self) -> bool:
        """An explicit abort, seen from the driver or a tool worker.

        Pi's abort lets the run reach its normal end: the current response or tool
        batch settles, then the run stops with an aborted assistant message. Caller
        task cancellation instead unwinds at once.
        """
        task = asyncio.current_task()
        return (
            self.token.cancelled
            and self.token.reason != CALLER_CANCELLED
            and not (task is not None and task.cancelling())
        )

    def checkpoint(self) -> None:
        if self.token.cancelled and not self.aborted():
            raise asyncio.CancelledError(self.token.reason)

    def cancelled_outcome(self) -> tuple[str, str, AssistantMessage | None]:
        """Status, reason and, for an explicit abort, Pi's closing aborted message."""
        if self.token.reason == "outcome_unknown":
            return (
                "failed",
                "outcome_unknown",
                failure_message(self, "error", "Child agent reported an unknown external outcome"),
            )
        status = "limit_reached" if self.token.reason == "run_timeout" else "cancelled"
        failure = failure_message(self, "aborted", abort_error(self)) if self.aborted() else None
        return status, self.token.reason or "cancelled", failure

    def stuck(self) -> bool:
        """A managed operation ignored cancellation past the cleanup deadline."""
        return any(not task.done() for task in self._owned)

    async def await_owned(
        self,
        awaitable: Awaitable,
        timeout: float | None = None,
        on_timeout: Callable[[], None] | None = None,
    ) -> Any:
        if timeout is None and self.token.cancelled:
            timeout = self.limits.cleanup_timeout
        task = asyncio.ensure_future(awaitable)
        self._owned.add(task)

        def done(future: asyncio.Future) -> None:
            self._owned.discard(future)
            if not future.cancelled():
                future.exception()  # retrieved even when the owner has already been cancelled

        task.add_done_callback(done)
        # A cancellation request also wakes an awaited end subscriber. The driver
        # remains the cleanup owner even when the caller cancels repeatedly.
        cancel_wait = None if self.token.cancelled else asyncio.create_task(self.token.wait())
        watched = {task} if cancel_wait is None else {task, cancel_wait}
        try:
            done_tasks, _ = await asyncio.wait(
                watched, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done_tasks:
                return task.result()
            cancelled = cancel_wait is not None and cancel_wait in done_tasks
            if not cancelled and on_timeout is not None:
                on_timeout()
            # Task cancellation is Python's abort signal. As in Pi, the operation may
            # finish its own way; it gets the cleanup deadline to do so.
            task.cancel()
            await asyncio.wait({task}, timeout=self.limits.cleanup_timeout)
            if (
                cancelled
                and task.done()
                and not task.cancelled()
                and self.token.reason != CALLER_CANCELLED
            ):
                return task.result()
            if cancelled:
                raise asyncio.CancelledError(self.token.reason)
            raise TimeoutError("Operation deadline exceeded")
        finally:
            if cancel_wait is not None:
                cancel_wait.cancel()
                await asyncio.gather(cancel_wait, return_exceptions=True)

    async def emit(self, kind: str, call_id: str | None = None, **data: Any) -> None:
        await self.agent._events.emit(kind, self.id, self.turn, data, call_id, self.await_owned)

    async def hook(self, name: str, *args: Any) -> Any:
        callback = getattr(self.hooks, name)
        if callback is None:
            return None
        return await self.await_owned(invoke(callback, *args))

    async def poll_queue(self, steering: bool) -> list[Message]:
        agent = self.agent
        callback = agent._get_steering_messages if steering else agent._get_follow_up_messages
        if callback is not None:
            values = await self.await_owned(invoke(callback)) or []
            validate_history(values)
            return deepcopy(values)
        return agent._queues.take(steering)

    async def append(self, message: Message, *, started: bool = False) -> None:
        data = message_to_dict(message)
        # Commit before publishing; a recorder fault cannot erase an observed execution.
        self.agent._messages.append(deepcopy(message))
        self.context.messages.append(deepcopy(message))
        self.context.new_messages.append(deepcopy(message))
        if not started:
            await self.emit("message_start", message=data)
        await self.emit("message_end", message=data)

    async def pending(self, pending: list[Message]) -> None:
        """Commit new input, declaring tool changes in a system message first."""
        messages = declare_tool_changes(
            self.context.messages, pending, [t.declaration() for t in self.context.tools]
        )
        for message in messages:
            before = len(self.agent._messages)
            try:
                await self.append(message)
            finally:
                if len(self.agent._messages) > before:
                    self.agent._queues.committed(message, pending)

    async def prepare(self, name: str) -> list[Message]:
        """Run a prepare hook; its update persists for the rest of this run."""
        update = await self.hook(name, deepcopy(self.context), self.token)
        if update is None:
            return []
        if not isinstance(update, TurnUpdate):
            raise ConfigurationError(f"{name} must return TurnUpdate or None")
        self.agent._validate_update(update)
        merge(self.context, update)
        if update.context is not None:
            validate_history(update.context)
            self.context.messages = deepcopy(update.context)
        validate_history(update.messages)
        return deepcopy(update.messages)

    async def apply_updates(self) -> None:
        """Explicit update_config calls made while running take effect at a turn boundary."""
        updates = self.agent._updates
        while updates:
            update = updates.pop(0)
            merge(self.agent._defaults, update)
            merge(self.context, update)
            model = update.model
            await self.emit(
                "config_update",
                model=getattr(model, "id", model),
                options=update.options,
                tools=None if update.tools is None else [t.name for t in update.tools],
            )

    async def cleanup(self) -> None:
        tasks = set(self._owned)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.limits.cleanup_timeout)
            if pending:
                agent = self.agent
                agent._cleanup_complete = False
                self.fail("CleanupTimeoutError: managed tasks did not finish")

                # A blocking tool cannot be interrupted, but it does finish eventually;
                # the instance becomes usable again once nothing it owns is running.
                def finished(task: asyncio.Future) -> None:
                    pending.discard(task)
                    if not pending:
                        agent._cleanup_finished()

                for task in list(pending):
                    task.add_done_callback(finished)

    async def commit_outcome(self, outcome: ToolOutcome) -> None:
        if id(outcome) in self._committed:
            return
        if outcome.result is None:
            if outcome.execution_status == "running":
                outcome.result = error_result("not_stopped", "Tool did not stop after cancellation")
            elif outcome.raw_result is not None:
                outcome.result = error_result(
                    "finalization_cancelled", "Execution finished; finalization interrupted"
                )
            else:
                outcome.result = error_result("cancelled", "Tool call did not start")
        if outcome.execution_status == "unknown":
            self.agent._unknown = True
        result = outcome.result
        outcome._settled = True
        self._committed.add(id(outcome))
        message = ToolResultMessage(
            outcome.call.id,
            outcome.call.name,
            deepcopy(result.content),
            result.is_error,
            details=deepcopy(result.details),
            usage=deepcopy(result.usage),
            nested_calls=deepcopy(result.nested_calls),
        )
        # Persist even when delivery fails; then stop further external execution.
        await self.append(message)

    async def end_tool(self, outcome: ToolOutcome) -> None:
        if id(outcome) in self._ended:
            return
        self._ended.add(id(outcome))
        await self.emit(
            "tool_execution_end",
            outcome.call.id,
            result=asdict(outcome.result) if outcome.result else None,
            execution_status=outcome.execution_status,
        )

    async def drive(self, pending: list[Message]) -> RunResult:
        agent = self.agent
        self.driver_started = True
        status, reason = "completed", "stop"
        errors: list[str] = []
        # Pi records a run that stopped outside a model response as one more failed
        # or aborted assistant message, followed by turn_end.
        failure: AssistantMessage | None = None
        try:
            status, reason = await run_loop(self, pending)
        except asyncio.CancelledError:
            status, reason, failure = self.cancelled_outcome()
        except Exception as exc:
            if isinstance(exc, TimeoutError) and self.aborted():
                # A bounded step that outlived the cleanup deadline after abort is part of the abort.
                status, reason, failure = self.cancelled_outcome()
            else:
                status, reason = "failed", type(exc).__name__
                self.fail(f"{type(exc).__name__}: {exc}")
                errors.append(agent._last_error or reason)
                if not isinstance(exc, SubscriptionError):  # the recorder is broken; stop
                    failure = failure_message(self, "error", agent._last_error)
        self.finalizing = True
        # Finalization has an independent owner: repeated abort cannot interrupt it.
        self.token.cancel(reason) if status != "completed" else None
        await self.cleanup()
        for outcome in self.active_batch:
            try:
                await self.commit_outcome(outcome)
                await self.end_tool(outcome)
            except asyncio.CancelledError:
                status = "cancelled"
                errors.append("Result event delivery cancelled")
            except Exception as exc:
                status = "failed"
                errors.append(f"{type(exc).__name__}: {exc}")
        tail = agent._messages[-1] if len(agent._messages) > self.start else None
        if failure is not None and not (
            isinstance(tail, AssistantMessage) and tail.stop_reason in {"error", "aborted"}
        ):
            try:
                if failure.stop_reason == "aborted":
                    self.fail(failure.error or "aborted")
                await self.append(failure)
                await self.emit("turn_end", message=message_to_dict(failure), tool_results=[])
            except asyncio.CancelledError:
                status = "cancelled" if status != "limit_reached" else status
                errors.append("Failure message delivery cancelled")
            except Exception as exc:
                status = "failed"
                errors.append(f"{type(exc).__name__}: {exc}")
        if agent._unknown:
            status, reason = (
                (
                    "cancelled"
                    if status == "cancelled" and self.token.reason != "outcome_unknown"
                    else "failed"
                ),
                "outcome_unknown",
            )
        if agent._last_error and agent._last_error not in errors:
            errors.append(agent._last_error)
        if not agent._cleanup_complete and agent._last_error not in errors:
            errors.append(agent._last_error or "CleanupTimeoutError")
        self.partial = None
        try:
            # End delivery is itself bounded on cancellation/error paths.
            if status == "completed" or status == "limit_reached" and reason != "run_timeout":
                await self.emit("agent_end", status=status, stop_reason=reason)
            elif agent._cleanup_complete:
                await self.await_owned(
                    self.emit("agent_end", status=status, stop_reason=reason),
                    self.limits.cleanup_timeout,
                )
        except asyncio.CancelledError:
            status = (
                "failed"
                if self.token.reason == "outcome_unknown"
                else "limit_reached"
                if self.token.reason == "run_timeout"
                else "cancelled"
            )
            reason = self.token.reason or "cancelled"
            await self.cleanup()
        except Exception as exc:
            status = "failed" if status != "cancelled" else status
            errors.append(f"{type(exc).__name__}: {exc}")
            self.fail(errors[-1])
            await self.cleanup()
        # Explicit config updates persist even if no further model request occurred.
        for update in agent._updates:
            merge(agent._defaults, update)
        agent._updates.clear()
        usage: dict[str, Any] = {}
        for m in agent._messages[self.start :]:
            if isinstance(m, AssistantMessage):
                for key, value in m.usage.items():
                    if type(value) in (int, float):
                        usage[key] = usage.get(key, 0) + value
        # Inputs selected for a later boundary but never committed remain queued.
        agent._queues.restore()
        return RunResult(
            status,
            deepcopy(agent._messages[self.start :]),
            usage,
            reason,
            errors,
            agent._unknown,
            deepcopy(self.outcomes),
            agent._cleanup_complete,
            len(agent._queues.steering),
            len(agent._queues.follow_up),
        )
