"""Blocking entry points for plain scripts.

All blocking calls share one private event loop in a daemon thread. Agents, providers and
their cached connections therefore stay on the same loop from one call to the next, which
separate ``asyncio.run`` calls would break. Code that already runs an event loop (servers,
notebooks with top-level await) should await the async API instead.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

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
