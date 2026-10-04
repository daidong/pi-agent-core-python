"""Combine the application's hooks with the handlers plugins register for the same slot.

The rules follow Pi's extension runner (``runner.ts``). Handlers run in order: the
application's own hook first, then plugins in load order, each plugin's handlers in
registration order.

* ``before_tool_call``: the first handler that blocks (False) or answers with a ToolResult
  decides; the rest are not called. An exception fails that tool call, as in Pi.
* ``after_tool_call``, ``transform_context``, ``convert_to_llm``, ``on_payload``: chained;
  each handler sees the previous one's output.
* ``prepare_request`` / ``prepare_next_turn``: each handler sees the model, options, tools
  and context set by the handlers before it; all new messages are appended afterwards.
* ``finish_turn``: every handler runs; "end" beats "continue".
* ``get_api_key``: the first key returned wins.
* ``on_response``, ``on_provider_stream_event`` and event listeners: every handler runs.

A plugin handler that raises (outside ``before_tool_call``) or returns the wrong type is
reported through ``on_error`` and skipped, so one faulty plugin does not break the others.
The application's own hook keeps the core behavior: its exceptions propagate, and a value
the core would reject is passed on for the core to reject.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from copy import copy, deepcopy
from dataclasses import dataclass, fields
from typing import Any

from ..errors import ConfigurationError, SubscriptionError
from ..events import Event, EventListener
from ..hooks import Hooks, RunContext, TurnUpdate
from ..tools import ToolResult, ToolResultUpdate, apply_result_update, invoke

HOOK_NAMES = tuple(f.name for f in fields(Hooks))
logger = logging.getLogger("pi_python.plugins")


@dataclass(frozen=True)
class PluginFailure:
    """A report that a plugin handler failed; the run continued without its contribution.

    This is not an exception: it is what ``on_error`` receives. `error` is the exception.
    """

    plugin: str
    hook: str
    error: BaseException

    def __str__(self) -> str:
        return f"plugin {self.plugin!r} failed in {self.hook}: {type(self.error).__name__}: {self.error}"


def log_plugin_failure(failure: PluginFailure) -> None:
    logger.error("%s", failure, exc_info=failure.error)


# An entry is (plugin name or None for the application, handler).
Entry = tuple[str | None, Callable[..., Any]]
Report = Callable[[PluginFailure], Awaitable[None]]


class _Runner:
    def __init__(self, hook: str, entries: Sequence[Entry], report: Report):
        self.hook = hook
        self.entries = list(entries)
        self.report = report

    async def call(
        self, owner: str | None, handler: Callable[..., Any], *args: Any
    ) -> tuple[bool, Any]:
        """Run one handler; a plugin's failure is reported and gives (False, None)."""
        try:
            return True, await invoke(handler, *args)
        except SubscriptionError:
            raise
        except Exception as exc:
            if owner is None:
                raise
            await self.report(PluginFailure(owner, self.hook, exc))
            return False, None

    async def wrong(self, owner: str | None, message: str) -> None:
        """A handler returned a value of the wrong type: the core's error for the
        application's own hook, a report for a plugin's."""
        error = ConfigurationError(f"{self.hook} {message}")
        if owner is None:
            raise error
        await self.report(PluginFailure(owner, self.hook, error))


def _before_tool_call(r: _Runner) -> Callable[..., Any]:
    async def before_tool_call(call: Any, args: Any, context: Any) -> Any:
        for owner, handler in r.entries:
            decision = await invoke(handler, deepcopy(call), deepcopy(args), context)
            if decision is False or isinstance(decision, ToolResult):
                return decision
            if decision is not None and decision is not True:
                if owner is None:
                    return decision  # the core rejects it, as it would without plugins
                # As an exception from a plugin would, a malformed decision fails the call.
                raise ConfigurationError(
                    f"before_tool_call of plugin {owner!r} must return bool, ToolResult or None"
                )
        return None

    return before_tool_call


def _after_tool_call(r: _Runner) -> Callable[..., Any]:
    async def after_tool_call(call: Any, result: ToolResult, context: Any) -> Any:
        current, changed = deepcopy(result), False
        for owner, handler in r.entries:
            context.result, context.is_error = deepcopy(current), current.is_error
            ok, value = await r.call(owner, handler, deepcopy(call), deepcopy(current), context)
            if not ok or value is None:
                continue
            if isinstance(value, ToolResultUpdate):
                apply_result_update(current, value)
            elif isinstance(value, ToolResult):
                current = deepcopy(value)
            elif owner is None:
                raise TypeError("after_tool_call must return ToolResult, ToolResultUpdate or None")
            else:
                await r.wrong(owner, "must return ToolResult, ToolResultUpdate or None")
                continue
            changed = True
        return current if changed else None

    return after_tool_call


def _message_chain(r: _Runner, with_cancel: bool) -> Callable[..., Any]:
    async def chain(messages: list[Any], *rest: Any) -> list[Any]:
        current = messages
        for owner, handler in r.entries:
            args = (deepcopy(current), *rest) if with_cancel else (deepcopy(current),)
            ok, value = await r.call(owner, handler, *args)
            if owner is None and not isinstance(value, list):
                return value  # the core rejects it, as it would without plugins
            if not ok or value is None:
                continue
            if not isinstance(value, list):
                await r.wrong(owner, "must return a list of messages or None")
                continue
            current = value
        return current

    return chain


def _prepare(r: _Runner) -> Callable[..., Any]:
    async def prepare(context: RunContext, cancel: Any) -> TurnUpdate | None:
        working = deepcopy(context)
        combined: TurnUpdate | None = None
        for owner, handler in r.entries:
            ok, update = await r.call(owner, handler, deepcopy(working), cancel)
            if not ok or update is None:
                continue
            if not isinstance(update, TurnUpdate):
                await r.wrong(owner, "must return TurnUpdate or None")
                continue
            combined = combined or TurnUpdate()
            for key in ("model", "options", "tools"):
                value = getattr(update, key)
                if value is not None:
                    setattr(combined, key, deepcopy(value))
                    setattr(working, key, deepcopy(value))
            if update.context is not None:
                combined.context = deepcopy(update.context)
                working.messages = deepcopy(update.context)
            combined.messages.extend(deepcopy(update.messages))
        return combined

    return prepare


def _finish_turn(r: _Runner) -> Callable[..., Any]:
    async def finish_turn(context: RunContext, cancel: Any) -> str | None:
        decisions = set()
        for owner, handler in r.entries:
            ok, decision = await r.call(owner, handler, deepcopy(context), cancel)
            if not ok:
                continue
            if decision not in {None, "continue", "end"}:
                await r.wrong(owner, "must return continue, end or None")
                continue
            decisions.add(decision)
        return "end" if "end" in decisions else "continue" if "continue" in decisions else None

    return finish_turn


def _get_api_key(r: _Runner) -> Callable[..., Any]:
    async def get_api_key(provider: str) -> Any:
        for owner, handler in r.entries:
            ok, key = await r.call(owner, handler, provider)
            if ok and key is not None:
                return key
        return None

    return get_api_key


def _on_payload(r: _Runner) -> Callable[..., Any]:
    async def on_payload(payload: Any) -> Any:
        current, replaced = payload, False
        for owner, handler in r.entries:
            ok, value = await r.call(owner, handler, current)
            if not ok or value is None:
                continue
            if not isinstance(value, dict):
                await r.wrong(owner, "must return a dict or None")
                continue
            current, replaced = value, True
        return current if replaced else None

    return on_payload


def _observe(r: _Runner) -> Callable[..., Any]:
    async def observe(value: Any) -> None:
        for owner, handler in r.entries:
            await r.call(owner, handler, value)

    return observe


_COMPOSERS: dict[str, Callable[[_Runner], Callable[..., Any]]] = {
    "before_tool_call": _before_tool_call,
    "after_tool_call": _after_tool_call,
    "transform_context": lambda r: _message_chain(r, with_cancel=True),
    "convert_to_llm": lambda r: _message_chain(r, with_cancel=False),
    "prepare_request": _prepare,
    "prepare_next_turn": _prepare,
    "finish_turn": _finish_turn,
    "get_api_key": _get_api_key,
    "on_payload": _on_payload,
    "on_response": _observe,
    "on_provider_stream_event": _observe,
}
assert set(_COMPOSERS) == set(HOOK_NAMES)


def compose_hooks(
    base: Hooks | None, handlers: Sequence[tuple[str, str, Callable[..., Any]]], report: Report
) -> Hooks:
    """One Hooks object: `base` (the application's) followed by (plugin, hook, handler)."""
    composed = copy(base) if base else Hooks()
    for name in HOOK_NAMES:
        plugin_entries: list[Entry] = [(p, h) for p, hook, h in handlers if hook == name]
        if not plugin_entries:
            continue
        own = getattr(composed, name)
        entries: list[Entry] = ([(None, own)] if own else []) + plugin_entries
        setattr(composed, name, _COMPOSERS[name](_Runner(name, entries, report)))
    return composed


def isolated_listener(plugin: str, listener: EventListener, report: Report) -> EventListener:
    """An event subscriber whose failure is reported instead of stopping the run."""

    async def guarded(event: Event) -> None:
        try:
            await invoke(listener, event)
        except Exception as exc:
            await report(PluginFailure(plugin, f"event {event.type}", exc))

    return guarded
