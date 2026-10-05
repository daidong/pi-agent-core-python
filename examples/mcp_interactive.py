"""Offline, runnable sampling + form elicitation demo (no API key needed).

    uv run --extra mcp-interactive python examples/mcp_interactive.py

The deterministic UI below simulates a user choosing a form answer. Replace choose()
with your async GUI handler, and DemoProvider with your host-owned model provider.
The server is a separate process and uses only the public SamplingProvider and MCP SDK.
"""

import asyncio
import argparse
import sys
from pathlib import Path

from pi_python import (
    AssistantMessage,
    CancelToken,
    ModelEvent,
    ToolCall,
    ToolContext,
    ToolResultMessage,
    TextContent,
)
from pi_python.mcp import (
    ElicitationHandler,
    ElicitationResponse,
    MCPCallbacks,
    SamplingHandler,
    SamplingProfile,
    SamplingRetryPolicy,
    MCP_FEATURES,
)
from pi_python.plugins import Plugin, load_plugins


class DemoProvider:
    async def stream(self, request, cancel):
        cancel.raise_if_cancelled()
        if not request.tools:
            yield ModelEvent.done(
                AssistantMessage(
                    [
                        TextContent(
                            '{"ok": true}' if "text" in request.options else "Ready",
                            "vendor-signature",
                        )
                    ],
                    usage={"input": 4, "output": 3, "cacheRead": 1},
                )
            )
            return
        results = [m for m in request.messages if isinstance(m, ToolResultMessage)]
        if results:
            yield ModelEvent.done(
                AssistantMessage.text(" ".join(m.content[0].text for m in results))
            )
        else:
            label = request.messages[-1].content[0].text
            yield ModelEvent.done(
                AssistantMessage(
                    [
                        ToolCall("first", "decorate", {"text": label}),
                        ToolCall("second", "decorate", {"text": "ready"}),
                    ],
                    stop_reason="tool_use",
                )
            )


async def choose(request, cancel):
    print(f"Form from {request.context.server}: {request.message}")
    print("Demo user chooses: short, confirmed")
    cancel.raise_if_cancelled()
    return ElicitationResponse("accept", {"style": "short", "confirmed": True})


async def main(url=None):
    provider = DemoProvider()  # Owned by this host; pi-python will not close it.
    assert "sampling-host-state-v1" in MCP_FEATURES

    async def prepare(context, profile, request):
        # With a real Provider, this is where the host injects request.api_key and
        # on_payload/on_response/on_provider_stream_event. None crosses MCP.
        assert context.server == "business"

    async def observe(event):
        # Charge at the actual Provider, using this task/reverse-request pair.
        # None means unknown usage; it must not be recorded as a measured zero.
        print(
            f"Model task={event.context.run_id} request={event.context.request_id} "
            f"profile={event.profile} attempt={event.attempt} usage={event.usage}"
        )

    callbacks = MCPCallbacks(
        sampling=SamplingHandler(
            provider,
            model="offline-demo",
            prepare=prepare,
            observe=observe,
            retry=SamplingRetryPolicy(max_attempts=2),
            profiles={
                "json": SamplingProfile(
                    "offline-demo", {"text": {"format": {"type": "json_object"}}}
                ),
                "text": SamplingProfile("offline-demo"),
            },
        ),
        elicitation=ElicitationHandler(choose),
    )

    def setup(api):
        transport = (
            {"url": url}
            if url
            else {
                "command": sys.executable,
                "args": [str(Path(__file__).with_name("mcp_interactive_server.py"))],
            }
        )
        api.add_mcp_server(
            "business",
            {
                **transport,
                "required_capabilities": [
                    "sampling.tools",
                    "sampling.host_state",
                    "sampling.profiles",
                    "elicitation.form",
                ],
            },
        )

    async with load_plugins(
        Plugin("demo", setup),
        strict=True,
        mcp_callbacks={("demo", "business"): callbacks},
    ) as plugins:
        status = await plugins.readiness(
            required_tools=["mcp__business__workflow"],
            required_mcp_capabilities={"business": ["sampling.tools", "elicitation.form"]},
        )
        status.require_ready()

        async def progress(value):
            pass

        workflow = next(t for t in plugins.tools if t.name == "mcp__business__workflow")
        result = await workflow.execute(
            {"label": "report", "auxiliary": True},
            ToolContext("demo-run", "workflow", CancelToken(), progress),
        )
        if result.is_error:
            raise RuntimeError(result.content[0].text)
        print(result.content[0].text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Connect to an existing streamable HTTP server")
    asyncio.run(main(parser.parse_args().url))
