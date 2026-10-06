"""Ownership of asynchronous work independent of an Agent or transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable
from typing import Any, TypeVar

from .cancellation import CancelToken

T = TypeVar("T")


def _cancel(task: asyncio.Future[Any], reason: str | None = None) -> None:
    if not task.done() and (not isinstance(task, asyncio.Task) or not task.cancelling()):
        task.cancel(reason)


async def _join(tasks: Iterable[asyncio.Future[Any]]) -> None:
    """Finish cleanup even if the caller is cancelled again, then propagate it."""
    pending = asyncio.gather(*tasks, return_exceptions=True)
    interrupted = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError


async def _gather_owned(awaitables: Iterable[Awaitable[T]]) -> list[T]:
    """A batch owns every child until it exits, including after the first failure."""
    tasks = [asyncio.ensure_future(value) for value in awaitables]
    group = asyncio.gather(*tasks)
    try:
        return await asyncio.shield(group)
    finally:
        for task in tasks:
            _cancel(task)
        # Retrieve the group exception too if caller cancellation won the race.
        await _join([group, *tasks])


async def _finish(awaitable: Awaitable[T]) -> T:
    """Give cleanup one owner; caller cancellation is delivered after it finishes."""
    task = asyncio.ensure_future(awaitable)
    await _join([task])
    return task.result()


class TaskScope:
    """Own calls made with ``run`` and cancel/join them on ``aclose``.

    Each call runs in its own task. Cancellation of its caller or optional token
    cancels that task and waits for its cleanup. Closing rejects new calls, cancels
    active calls (including work waiting on application locks), and joins them.
    Exceptions are delivered to each caller, not to unrelated calls or ``aclose``.
    The scope is bound to its first event loop and cannot be reopened. Work must
    cooperate with cancellation; there is no forced task termination or deadline.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Future[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("TaskScope must be used on its owning event loop")
        self._loop = loop

    async def __aenter__(self) -> TaskScope:
        self._bind()
        if self._closed:
            raise RuntimeError("TaskScope is closed")
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def run(self, awaitable: Awaitable[T], *, cancel: CancelToken | None = None) -> T:
        try:
            self._bind()
            if self._closed:
                raise RuntimeError("TaskScope is closed")
            if cancel is not None:
                cancel.raise_if_cancelled()
            if isinstance(awaitable, asyncio.Future) and awaitable.get_loop() is not self._loop:
                raise RuntimeError("TaskScope cannot adopt work from another event loop")
        except BaseException:
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            raise
        task = asyncio.ensure_future(awaitable)
        if task is asyncio.current_task():
            raise RuntimeError("TaskScope cannot run its caller task")
        self._tasks.add(task)
        watcher = None
        if cancel is not None:

            async def watch() -> None:
                await cancel.wait()
                _cancel(task, cancel.reason)

            watcher = asyncio.create_task(watch())
        try:
            return await asyncio.shield(task)
        finally:
            _cancel(task)
            children: list[asyncio.Future[Any]] = [task]
            if watcher is not None:
                watcher.cancel()
                children.append(watcher)
            try:
                await _join(children)
            finally:
                self._tasks.discard(task)

    async def aclose(self) -> None:
        self._bind()
        if asyncio.current_task() in self._tasks:
            raise RuntimeError("An owned task cannot close its own TaskScope")
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            _cancel(task)
        await _join(tasks)
