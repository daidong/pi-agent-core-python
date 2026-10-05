import asyncio
import inspect

import pytest

from pi_python import CancelToken, TaskScope


async def test_result_exception_and_closed_rejection():
    scope = TaskScope()
    async with scope:
        assert await scope.run(asyncio.sleep(0, result=7)) == 7

        async def fail():
            raise ValueError("expected")

        with pytest.raises(ValueError, match="expected"):
            await scope.run(fail())
        assert await scope.run(asyncio.sleep(0, result=8)) == 8
    unused = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="closed"):
        await scope.run(unused)
    assert inspect.getcoroutinestate(unused) == inspect.CORO_CLOSED
    await scope.aclose()


@pytest.mark.parametrize("mode", ["token", "caller", "close"])
async def test_cancel_joins_cleanup_and_closes_queued_work(mode):
    scope = TaskScope()
    token = CancelToken()
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    completed = []
    lock = asyncio.Lock()

    async def work():
        async with lock:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish.wait()
                completed.append("clean")

    running = asyncio.create_task(scope.run(work(), cancel=token))
    await started.wait()
    queued_entered = []

    async def queued():
        async with lock:
            queued_entered.append(True)

    queued_token = CancelToken()
    queued_call = asyncio.create_task(scope.run(queued(), cancel=queued_token))
    await asyncio.sleep(0)
    queued_token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued_call
    closer = None
    if mode == "token":
        token.cancel()
    elif mode == "caller":
        running.cancel()
    else:
        closer = asyncio.create_task(scope.aclose())
    await cleaning.wait()
    assert not running.done()
    # A second caller cancellation must not interrupt the owned task's cleanup.
    running.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    if closer:
        await closer
    await scope.aclose()
    assert completed == ["clean"] and not queued_entered


async def test_precancelled_call_never_starts():
    token = CancelToken()
    token.cancel()
    coro = asyncio.sleep(0)
    async with TaskScope() as scope:
        with pytest.raises(asyncio.CancelledError):
            await scope.run(coro, cancel=token)
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


async def test_cancelled_close_still_joins_cleanup():
    scope = TaskScope()
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()

    call = asyncio.create_task(scope.run(work()))
    await started.wait()
    closer = asyncio.create_task(scope.aclose())
    await cleaning.wait()
    closer.cancel()
    await asyncio.sleep(0)
    assert not closer.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await closer
    with pytest.raises(asyncio.CancelledError):
        await call
    await scope.aclose()


async def test_owned_task_cannot_close_its_scope():
    async with TaskScope() as scope:
        with pytest.raises(RuntimeError, match="own TaskScope"):
            await scope.run(scope.aclose())
