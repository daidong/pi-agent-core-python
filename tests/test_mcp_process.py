import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from pi_python import CancelToken, ConfigurationError, ToolContext
from pi_python.mcp import connect_stdio
from pi_python import _mcp_process as processes

SERVER = Path(__file__).parent / "servers" / "mcp_process_server.py"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process ownership")


@pytest.fixture(autouse=True)
def sdk():
    pytest.importorskip("mcp")
    if sys.implementation.name == "pypy":
        pytest.skip("SDK stdio server needs CPython")


async def emit(value):
    pass


async def call(tools, name="spawn", **args):
    tool = next(t for t in tools if t.name == name)
    result = await tool.execute(args, ToolContext("run", "call", CancelToken(), emit))
    assert not result.is_error
    return json.loads(result.content[0].text)


def alive(pid):
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


async def assert_stopped(pids):
    for _ in range(100):
        if all(not alive(pid) for pid in pids.values()):
            return
        await asyncio.sleep(0.02)
    assert all(not alive(pid) for pid in pids.values()), pids


def cleanup(pids):
    for pid in pids.values():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("exit_kind", ["normal", "error", "cancel"])
async def test_scope_cleans_children_after_server_exits(exit_kind):
    pids = {}
    ready = asyncio.Event()

    async def run():
        nonlocal pids
        async with connect_stdio(sys.executable, [str(SERVER)], process_scope=True) as tools:
            pids = await call(tools)
            ready.set()
            if exit_kind == "error":
                raise ValueError("caller failed")
            if exit_kind == "cancel":
                await asyncio.Event().wait()

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(ready.wait(), 15)
        if exit_kind == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 15)
        elif exit_kind == "error":
            with pytest.raises(ValueError, match="caller failed"):
                await task
        else:
            await task
        await assert_stopped(pids)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        cleanup(pids)


@pytest.mark.parametrize("crash", [False, True])
async def test_scope_owns_nested_connection_even_after_parent_crash(tmp_path, crash):
    marker = tmp_path / "pids.json"
    pids = {}
    try:
        if crash:
            with pytest.raises(Exception):
                async with connect_stdio(
                    sys.executable, [str(SERVER)], process_scope=True
                ) as tools:
                    await call(tools, "nested", marker=str(marker), crash=True)
        else:
            async with connect_stdio(sys.executable, [str(SERVER)], process_scope=True) as tools:
                pids = await call(tools, "nested", marker=str(marker))
                await assert_stopped({k: v for k, v in pids.items() if k != "outer"})
                assert alive(pids["outer"])
        pids = json.loads(marker.read_text())
        await assert_stopped(pids)
    finally:
        if marker.exists():
            cleanup(json.loads(marker.read_text()))


async def test_scope_cleans_failed_handshake(tmp_path):
    marker = tmp_path / "pids.json"
    try:
        with pytest.raises(TimeoutError):
            async with connect_stdio(
                sys.executable,
                [str(SERVER), "--no-handshake", str(marker)],
                process_scope=True,
                init_timeout=3,
            ):
                pytest.fail("unexpected handshake")
        await assert_stopped(json.loads(marker.read_text()))
    finally:
        if marker.exists():
            cleanup(json.loads(marker.read_text()))


async def test_independent_connections_are_isolated():
    first = second = {}
    try:
        async with connect_stdio(sys.executable, [str(SERVER)], process_scope=True) as a:
            first = await call(a)
            async with connect_stdio(sys.executable, [str(SERVER)], process_scope=True) as b:
                second = await call(b)
            await assert_stopped(second)
            assert all(alive(pid) for pid in first.values())
        await assert_stopped(first)
    finally:
        cleanup({**first, **{f"second_{k}": v for k, v in second.items()}})


async def test_inherited_sibling_connections_are_isolated():
    pids = {}
    try:
        async with connect_stdio(sys.executable, [str(SERVER)], process_scope=True) as tools:
            pids = await call(tools, "siblings")
            await assert_stopped(pids)
    finally:
        cleanup(pids)


async def test_plugin_configuration_owns_processes():
    from pi_python.plugins import Plugin, load_plugins

    def setup(api):
        api.add_mcp_server(
            "local", {"command": sys.executable, "args": [str(SERVER)], "process_scope": True}
        )

    pids = {}
    try:
        async with load_plugins(Plugin("lifecycle", setup), strict=True) as plugins:
            pids = await call(plugins.tools, "mcp__local__spawn")
        await assert_stopped(pids)
    finally:
        cleanup(pids)


async def test_repeated_cancellation_waits_for_reaper(monkeypatch, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = False

    async def reap(root, path):
        nonlocal completed
        entered.set()
        await release.wait()
        completed = True

    monkeypatch.setattr(processes, "_reap", reap)
    task = asyncio.create_task(processes._finish(tmp_path, tmp_path))
    await entered.wait()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed


async def test_corrupt_record_does_not_skip_other_groups(tmp_path, monkeypatch):
    (tmp_path / "lock").touch()
    (tmp_path / "group.json").write_text("invalid json")
    child = tmp_path / "child"
    child.mkdir()
    (child / "group.json").write_text(json.dumps({"pgid": 12345678}))
    signals = []

    def send(pgid, sig):
        signals.append((pgid, sig))
        return False

    monkeypatch.setattr(processes, "_signal", send)
    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        await processes._reap(tmp_path, tmp_path)
    assert signals == [(12345678, signal.SIGTERM)]


async def test_default_does_not_wrap_command(monkeypatch):
    monkeypatch.delenv(processes._ENV, raising=False)
    env = {"KEY": "value"}
    async with processes.process_scope("command", ["arg"], env, False) as actual:
        assert actual == ("command", ["arg"], env)


@pytest.mark.parametrize("enabled", [None, "true", 1])
async def test_scope_flag_requires_bool(enabled):
    with pytest.raises(ConfigurationError, match="boolean"):
        async with processes.process_scope("command", [], None, enabled):
            pass


async def test_closed_parent_refuses_new_connection(tmp_path, monkeypatch):
    (tmp_path / "lock").touch()
    (tmp_path / "closing").touch()
    monkeypatch.setenv(processes._ENV, json.dumps({"root": str(tmp_path), "path": str(tmp_path)}))
    with pytest.raises(RuntimeError, match="closing"):
        async with processes.process_scope("command", [], None, False):
            pass
