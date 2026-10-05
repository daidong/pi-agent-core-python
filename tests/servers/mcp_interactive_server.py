"""Exercise the runnable example, plus failure/lifecycle probes in a real process."""

import asyncio
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time

from mcp import types
from mcp.server.mcpserver import Context
from pi_python import (
    Agent,
    CancelToken,
    ModelRequest,
    UserMessage,
    tool,
    ToolContext,
    ToolResultMessage,
)
from pi_python.mcp import (
    SamplingProvider,
    SamplingHandler,
    MCPCallbacks,
    connect_stdio,
    connect_http,
)

demo = runpy.run_path(str(Path(__file__).parents[2] / "examples" / "mcp_interactive_server.py"))
server = demo["server"]
FORM = demo["FORM"]


@tool
def echo(value: str) -> str:
    """Echo the value once."""
    return value


@server.tool()
async def provider_loop(
    label: str, ctx: Context, followup: bool = False, profiles: bool = False
) -> str:
    async with SamplingProvider(ctx.request_context, require_host_state=True) as provider:
        agent = Agent(provider=provider, tools=[echo])
        result = await agent.prompt(label)
        if result.status != "completed":
            raise RuntimeError("Provider loop failed")
        if followup:
            result = await agent.prompt("continue")
            if result.status != "completed":
                raise RuntimeError("Provider followup failed")
        extra = []
        if profiles:
            for name in ("json", "text"):
                async for event in provider.stream(
                    ModelRequest([UserMessage(label)], options={"sampling_profile": name}),
                    CancelToken(),
                ):
                    extra.append(event.message.content[0].text)
        return json.dumps(
            {
                "text": result.messages[-1].content[0].text,
                "extra": extra,
                "tool_results": sum(isinstance(m, ToolResultMessage) for m in result.messages),
            }
        )


@server.tool()
async def sampling_error(ctx: Context) -> str:
    from mcp import MCPError

    async with SamplingProvider(ctx.request_context, require_host_state=True) as provider:
        try:
            async for _ in provider.stream(ModelRequest([UserMessage("failure")]), CancelToken()):
                pass
        except MCPError as exc:
            return json.dumps({"code": exc.code, "data": exc.data})
    return "unexpected success"


@server.tool()
async def nested(label: str, ctx: Context, child_url: str = "", marker: str = "") -> str:
    """A business process lends its scoped sampling Provider to a sandbox connection."""

    async def emit(event):
        pass

    try:
        async with SamplingProvider(ctx.request_context, require_host_state=True) as provider:
            callbacks = MCPCallbacks(sampling=SamplingHandler(provider, model="host"))
            connection = (
                connect_http(child_url, callbacks=callbacks, server_name="sandbox")
                if child_url
                else connect_stdio(
                    sys.executable,
                    [str(Path(__file__).resolve())],
                    callbacks=callbacks,
                    server_name="sandbox",
                )
            )
            async with connection as tools:
                inner = next(t for t in tools if t.name == "provider_loop")
                result = await inner.execute(
                    {"label": label}, ToolContext(label, label, CancelToken(), emit)
                )
                if result.is_error:
                    raise RuntimeError("Nested sandbox failed")
                return result.content[0].text
    finally:
        if marker:
            Path(marker).write_text("finished")


@server.tool()
async def sample(label: str, ctx: Context, marker: str = "") -> str:
    try:
        async with SamplingProvider(ctx.request_context) as provider:
            result = await Agent(provider=provider).prompt(label)
        return json.dumps({"status": result.status, "text": result.messages[-1].content[0].text})
    finally:
        if marker:
            Path(marker).write_text("finished")


@server.tool()
async def ask(label: str, ctx: Context, marker: str = "") -> str:
    try:
        response = await ctx.session.elicit_form(
            label,
            FORM,
            related_request_id=ctx.request_context.request_id,
        )
        return json.dumps(response.model_dump(exclude_none=True))
    finally:
        if marker:
            Path(marker).write_text("finished")


@server.tool()
async def raw_sample(label: str, ctx: Context) -> str:
    result = await ctx.session.create_message(
        [types.SamplingMessage(role="user", content=types.TextContent(type="text", text=label))],
        max_tokens=100,
        related_request_id=ctx.request_context.request_id,
    )
    return result.content.text


@server.tool()
def capabilities(ctx: Context) -> str:
    return json.dumps(
        {
            "capabilities": ctx.session.client_capabilities.model_dump(
                by_alias=True, exclude_none=True
            ),
            "protocol": ctx.request_context.protocol_version,
            "pid": os.getpid(),
        }
    )


@server.tool()
async def disconnect_during_callback(ctx: Context) -> str:
    async def crash():
        await asyncio.sleep(0.3)
        os._exit(23)

    asyncio.create_task(crash())
    await ctx.session.elicit_form(
        "before disconnect", FORM, related_request_id=ctx.request_context.request_id
    )
    return "unexpected"


@server.tool()
def stubborn_tree() -> str:
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(90)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    threading.Thread(target=lambda: time.sleep(90), daemon=False).start()
    return json.dumps({"parent": os.getpid(), "child": child.pid})


if __name__ == "__main__":
    if sys.argv[1:2] == ["--http"]:
        server.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        server.run()
