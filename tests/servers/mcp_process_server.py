"""Generic lifecycle fixtures: surviving children and abandoned nested connections."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

try:
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:
    from mcp.server.fastmcp import FastMCP as Server

from pi_python import CancelToken, ToolContext
from pi_python.mcp import connect_stdio

server = Server("process-lifecycle-test")


def spawn_child():
    # Detached stdio lets the MCP server itself exit gracefully. The child keeps
    # the server's process group and deliberately ignores graceful termination.
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(120)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert child.stdout.readline() == b"ready\n"
    child.stdout.close()
    return {"server": os.getpid(), "child": child.pid}


@server.tool()
def spawn() -> str:
    return json.dumps(spawn_child())


async def emit(value):
    pass


@server.tool()
async def nested(marker: str, crash: bool = False) -> str:
    # Explicit env intentionally omits the private ownership variable. The
    # adapter must inherit ownership even when callers sanitize server env.
    async with connect_stdio(sys.executable, [__file__], env={}) as tools:
        tool = next(t for t in tools if t.name == "spawn")
        result = await tool.execute({}, ToolContext("nested", "call", CancelToken(), emit))
        pids = {**json.loads(result.content[0].text), "outer": os.getpid()}
        Path(marker).write_text(json.dumps(pids))
        if crash:
            os._exit(7)
    return json.dumps(pids)


@server.tool()
async def siblings() -> str:
    async with connect_stdio(sys.executable, [__file__]) as first:
        context = ToolContext("siblings", "call", CancelToken(), emit)
        one = json.loads(
            (await next(t for t in first if t.name == "spawn").execute({}, context)).content[0].text
        )
        async with connect_stdio(sys.executable, [__file__]) as second:
            two = json.loads(
                (await next(t for t in second if t.name == "spawn").execute({}, context))
                .content[0]
                .text
            )
        # Closing one inherited connection must not signal its sibling's group.
        for pid in one.values():
            os.kill(pid, 0)
    return json.dumps({**one, **{f"second_{key}": value for key, value in two.items()}})


if __name__ == "__main__":
    if sys.argv[1:2] == ["--no-handshake"]:
        Path(sys.argv[2]).write_text(json.dumps(spawn_child()))
        asyncio.run(asyncio.sleep(120))
    else:
        server.run()
