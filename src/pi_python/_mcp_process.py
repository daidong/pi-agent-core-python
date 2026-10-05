"""Cooperative POSIX process ownership for nested stdio connections.

This file is also a stdlib-only exec launcher. It must remain runnable by absolute
path with Python's isolated mode, without importing the package or the MCP SDK.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile

_ENV = "PI_MCP_PROCESS_SCOPE"
_GRACE = 1.0


def _location(value: str) -> tuple[Path, Path]:
    data = json.loads(value)
    root, path = Path(data["root"]), Path(data["path"])
    if not root.is_absolute() or not path.is_absolute() or not path.is_relative_to(root):
        raise ValueError("Invalid MCP process scope")
    if root.resolve() != root or path.resolve() != path:
        raise ValueError("MCP process scope must not contain symlinks")
    return root, path


@contextmanager
def _locked(root: Path) -> Iterator[None]:
    import fcntl

    # Never recreate a removed scope: a late child must fail before exec.
    with (root / "lock").open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _require_open(root: Path, path: Path) -> None:
    for directory in (path, *path.parents):
        if not directory.is_dir() or (directory / "closing").exists():
            raise RuntimeError("MCP process scope is closing")
        if directory == root:
            return
    raise ValueError("Invalid MCP process scope")


def _launch(command: str, args: list[str]) -> None:
    root, path = _location(os.environ[_ENV])
    # SDK 2.x already creates a session. Older SDKs may not.
    if os.getpgrp() != os.getpid():
        os.setsid()
    with _locked(root):
        _require_open(root, path)
        # Publish before executing any server code. Readers hold the same lock.
        (path / "group.json").write_text(json.dumps({"pgid": os.getpid()}))
    os.execvpe(command, [command, *args], os.environ)


def _signal(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False


async def _reap(root: Path, path: Path) -> None:
    errors: list[Exception] = []
    groups: set[int] = set()
    with _locked(root):
        (path / "closing").touch()
        for entry in path.rglob("group.json"):
            try:
                pgid = json.loads(entry.read_text())["pgid"]
                if type(pgid) is not int or not 1 < pgid < 2**31 or pgid == os.getpgrp():
                    raise ValueError("Invalid MCP process group")
                groups.add(pgid)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(exc)
    # A bad record or failed signal must not prevent other groups being stopped.
    alive = set()
    for pgid in groups:
        try:
            if _signal(pgid, signal.SIGTERM):
                alive.add(pgid)
        except OSError as exc:
            errors.append(exc)
            alive.add(pgid)
    deadline = asyncio.get_running_loop().time() + _GRACE
    while alive and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
        for pgid in tuple(alive):
            try:
                if not _signal(pgid, 0):
                    alive.remove(pgid)
            except OSError:
                pass  # Still attempt SIGKILL below.
    for pgid in alive:
        try:
            _signal(pgid, signal.SIGKILL)
        except OSError as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup("MCP process cleanup failed", errors)


async def _finish(root: Path, path: Path) -> None:
    import anyio

    # AnyIO scopes and repeated native Task.cancel() both occur in host teardown.
    # Only this independent reaper runs in another task; SDK contexts still exit
    # in the task that entered them.
    cancelled = False
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(_reap(root, path))
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
    if cancelled:
        raise asyncio.CancelledError


@asynccontextmanager
async def process_scope(
    command: str, args: Sequence[str], env: dict[str, str] | None, enabled: bool
) -> AsyncIterator[tuple[str, list[str], dict[str, str] | None]]:
    from .errors import ConfigurationError

    if type(enabled) is not bool:
        raise ConfigurationError("process_scope must be a boolean")
    inherited = os.environ.get(_ENV)
    if not enabled and inherited is None:
        yield command, list(args), env
        return
    if os.name != "posix":
        raise ConfigurationError("process_scope requires POSIX process groups")
    if inherited is None:
        root = path = Path(tempfile.mkdtemp(prefix="pi-mcp-")).resolve()
        (root / "lock").touch()
    else:
        root, parent = _location(inherited)
        with _locked(root):
            _require_open(root, parent)
            path = Path(tempfile.mkdtemp(prefix="child-", dir=parent))
    child_env = dict(env or {})
    child_env[_ENV] = json.dumps({"root": str(root), "path": str(path)})
    error: BaseException | None = None
    try:
        yield sys.executable, ["-I", str(Path(__file__).resolve()), command, *args], child_env
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            await _finish(root, path)
        except Exception as exc:
            if error is None:
                raise
            error.add_note(f"MCP process cleanup also failed: {exc}")
        finally:
            # A child removes only its own subtree; parallel sibling connections
            # remain owned by their ancestor. Root ownership is connection-local.
            try:
                with _locked(root):
                    shutil.rmtree(path, ignore_errors=True)
            except FileNotFoundError:
                pass  # An ancestor may already have removed this subtree.


if __name__ == "__main__":
    _launch(sys.argv[1], sys.argv[2:])
