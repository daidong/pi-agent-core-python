"""Steering and follow-up queues, consumed at Pi's safe boundaries."""

from __future__ import annotations
from copy import deepcopy

from .errors import ConfigurationError
from .messages import Message

MODES = {"all", "one_at_a_time"}


class MessageQueues:
    """Two input queues. A taken message is reserved until history commits it.

    If a run ends before committing a reserved message, `restore` puts it back at the
    front of its queue, so an interrupted run never loses queued input.
    """

    def __init__(self, steering_mode: str = "one_at_a_time", follow_up_mode: str = "one_at_a_time"):
        if steering_mode not in MODES or follow_up_mode not in MODES:
            raise ConfigurationError("Invalid queue mode")
        self.steering_mode, self.follow_up_mode = steering_mode, follow_up_mode
        self.steering: list[Message] = []
        self.follow_up: list[Message] = []
        self._reserved: list[tuple[list[Message], Message]] = []

    def take(self, steering: bool) -> list[Message]:
        queue = self.steering if steering else self.follow_up
        mode = self.steering_mode if steering else self.follow_up_mode
        count = len(queue) if mode == "all" else min(1, len(queue))
        taken = queue[:count]
        del queue[:count]
        self._reserved.extend((queue, message) for message in taken)
        return taken

    def committed(self, message: Message, pending: list[Message]) -> None:
        """Release the reservation for a committed message from `pending`.

        Declaration reconciliation can change a system message's tool fields, so the
        match uses identity within `pending` plus timestamp and role.
        """
        for i, (_, reserved) in enumerate(self._reserved):
            if (
                any(reserved is item for item in pending)
                and reserved.timestamp == message.timestamp
                and reserved.role == message.role
            ):
                del self._reserved[i]
                return

    def restore(self) -> None:
        for queue, message in reversed(self._reserved):
            queue.insert(0, message)
        self._reserved.clear()

    def clear(self, *, steering: bool, follow_up: bool) -> None:
        if steering:
            self.steering.clear()
        if follow_up:
            self.follow_up.clear()
        self._reserved = [
            (queue, message)
            for queue, message in self._reserved
            if not (
                (steering and queue is self.steering) or (follow_up and queue is self.follow_up)
            )
        ]

    def peek(self) -> list[Message]:
        """What the next boundary would take: steering first, else follow-up."""
        steering = self.steering if self.steering_mode == "all" else self.steering[:1]
        follow_up = self.follow_up if self.follow_up_mode == "all" else self.follow_up[:1]
        return deepcopy(steering if steering else follow_up)

    def __bool__(self) -> bool:
        return bool(self.steering or self.follow_up)
