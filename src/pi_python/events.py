"""Sequential critical delivery, detached payloads and independent failure diagnostics."""

from __future__ import annotations
import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any
from .errors import MessageValidationError, SubscriptionError
from .messages import validate_json


@dataclass
class Event:
    type: str
    run_id: str
    turn_id: int
    sequence: int
    call_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1


EventListener = Callable[[Event], Awaitable[None] | None]
Unsubscribe = Callable[[], None]


def encode_event(event: Event) -> str:
    data = asdict(event)
    validate_json(data)
    decode_event(json.dumps(data))
    return json.dumps(data, ensure_ascii=False, allow_nan=False)


def decode_event(value: str) -> Event:
    try:
        d = json.loads(value)
        validate_json(d)
        if type(d.get("schema_version")) is not int or d["schema_version"] != 1:
            raise MessageValidationError("Unsupported event schema version")
        e = Event(**d)
        if (
            not isinstance(e.type, str)
            or not isinstance(e.run_id, str)
            or not isinstance(e.data, dict)
        ):
            raise MessageValidationError("Invalid event")
        if (
            type(e.sequence) is not int
            or e.sequence < 1
            or type(e.turn_id) is not int
            or e.turn_id < 0
        ):
            raise MessageValidationError("Invalid event sequence or turn")
        if e.call_id is not None and not isinstance(e.call_id, str):
            raise MessageValidationError("Invalid event call ID")
        return e
    except (ValueError, TypeError, AttributeError) as exc:
        raise MessageValidationError(str(exc)) from exc


class EventDispatcher:
    def __init__(self) -> None:
        self.listeners: list[EventListener] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.failed = False
        self.sequence = 0
        self.lock = asyncio.Lock()

    def subscribe(self, listener: EventListener) -> Unsubscribe:
        self.listeners.append(listener)
        active = True

        def unsubscribe() -> None:
            nonlocal active
            if active:
                active = False
                self.listeners.remove(listener)

        return unsubscribe

    async def emit(
        self,
        kind: str,
        run_id: str,
        turn_id: int,
        data: dict[str, Any],
        call_id: str | None,
        await_owned: Callable,
    ) -> None:
        if self.failed:
            return
        async with self.lock:
            if self.failed:
                return
            self.sequence += 1
            event = Event(kind, run_id, turn_id, self.sequence, call_id, deepcopy(data))
            encode_event(event)
            listeners = list(self.listeners)
            for index, listener in enumerate(listeners):
                try:
                    result = listener(deepcopy(event))
                    if inspect.isawaitable(result):
                        await await_owned(result)
                except Exception as exc:
                    self.failed = True
                    self.diagnostics.append(
                        {
                            "event": kind,
                            "sequence": event.sequence,
                            "listener_index": index,
                            "unhandled_listener_indices": list(range(index + 1, len(listeners))),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    raise SubscriptionError(f"{kind} subscriber {index} failed: {exc}") from exc


class EventQueue:
    """A bounded display adapter. Only message deltas may be dropped, explicitly counted."""

    def __init__(self, maxsize: int = 128, *, drop_text_updates: bool = False):
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize)
        self.drop_text_updates = drop_text_updates
        self.dropped_updates = 0

    async def __call__(self, event: Event) -> None:
        if self.drop_text_updates and event.type == "message_update" and self.queue.full():
            self.dropped_updates += 1
            return
        await self.queue.put(deepcopy(event))

    async def get(self) -> Event:
        return await self.queue.get()
