"""Blocking entry points for plain scripts.

``run_sync`` calls share one private event loop in a daemon thread. Agents, providers and
their cached connections therefore stay on the same loop from one call to the next, which
separate ``asyncio.run`` calls would break. Code that already runs an event loop (servers,
notebooks with top-level await) should await the async API instead.
``LoopPortal`` instead submits work from worker threads to a borrowed existing loop.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .tasks import TaskScope

T = TypeVar("T")


class LoopPortal:
    """Blocking calls from worker threads onto an existing asyncio event loop.

    Construct and close on the owning loop, normally with ``async with``. ``call``
    accepts an async callable and its arguments, so rejected or cancelled queued
    calls never leave an unawaited coroutine. It preserves the submitting thread's
    context variables. Closing cancels and joins submitted async work, but never
    stops the borrowed loop. Blocking calls from any event-loop thread are refused.
    """

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._scope = TaskScope()

    async def __aenter__(self) -> LoopPortal:
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("LoopPortal must be opened on its owning event loop")
        await self._scope.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("LoopPortal must be closed on its owning event loop")
        await self._scope.aclose()

    def call(
        self,
        function: Callable[..., Awaitable[T]],
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> T:
        """Submit and block; timeout/interruption requests cancellation of async work.

        A timeout bounds the blocking wait, not asynchronous cleanup. Keep the portal
        alive until ``aclose`` finishes to guarantee that cleanup has completed.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("LoopPortal.call cannot block an event-loop thread")
        if self._scope.closed or self.loop.is_closed() or not self.loop.is_running():
            raise RuntimeError("LoopPortal is closed or its event loop is not running")

        async def invoke() -> T:
            # Check on the owning loop too: aclose may race with submission.
            if self._scope.closed:
                raise RuntimeError("LoopPortal is closed")
            return await self._scope.run(function(*args, **kwargs))

        coroutine = invoke()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException:
            coroutine.close()
            raise
        try:
            return future.result(timeout)
        finally:
            if not future.done():
                future.cancel()


_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None


def _portal() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop is None or _thread is None or not _thread.is_alive():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="pi-python-sync", daemon=True)
            thread.start()
            _loop, _thread = loop, thread
        return _loop


@atexit.register
def _shutdown() -> None:
    if _loop is not None and _thread is not None and _thread.is_alive():
        _loop.call_soon_threadsafe(_loop.stop)
        _thread.join(timeout=1)


def run_sync(awaitable: Awaitable[T], *, on_interrupt: Callable[[], Any] | None = None) -> T:
    """Run an awaitable on the shared loop and block until it finishes.

    On Ctrl+C, ``on_interrupt`` runs on that loop (for example ``agent.abort``); the call
    waits for the awaitable to settle, then re-raises KeyboardInterrupt. A second Ctrl+C
    stops waiting.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()  # never awaited; avoid a "coroutine was never awaited" warning
        raise RuntimeError(
            "Blocking call inside a running event loop; await the async API instead "
            "(for example `await agent.prompt(...)`)"
        )

    async def wrapper() -> T:
        return await awaitable

    loop = _portal()
    future = asyncio.run_coroutine_threadsafe(wrapper(), loop)
    try:
        return future.result()
    except KeyboardInterrupt:
        if on_interrupt is not None:
            loop.call_soon_threadsafe(on_interrupt)
        else:
            loop.call_soon_threadsafe(future.cancel)
        try:
            future.exception()
        except BaseException:
            pass
        raise
