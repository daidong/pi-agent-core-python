"""Independent MCP business process; no model keys or custom host RPC code.

Run with mcp_interactive.py, or `python examples/mcp_interactive_server.py --http PORT`.
Requires pi-python-core[mcp-interactive].
"""

import json
import sys

from mcp.server.mcpserver import Context, MCPServer

from pi_python import Agent, CancelToken, ModelRequest, RunLimits, UserMessage, tool
from pi_python.mcp import SamplingProvider

server = MCPServer("interactive-demo")

FORM = {
    "type": "object",
    "properties": {
        "style": {"type": "string", "enum": ["short", "full"], "title": "Output style"},
        "confirmed": {"type": "boolean", "title": "Use this result?"},
        "note": {"type": "string", "maxLength": 80, "title": "Optional note"},
    },
    "required": ["style", "confirmed"],
}


@tool
def decorate(text: str) -> str:
    """Format one piece of business output."""
    return f"[{text}]"


@server.tool()
async def workflow(label: str, ctx: Context, auxiliary: bool = False) -> str:
    """Use the host model, execute local tools, and ask the user before returning."""
    async with SamplingProvider(ctx.request_context, require_host_state=True) as provider:
        agent = Agent(provider=provider, tools=[decorate], limits=RunLimits(max_model_requests=4))
        result = await agent.prompt(label)
        if result.status != "completed":
            raise RuntimeError(f"Business agent ended with status {result.status}")
        extra = {}
        if auxiliary:
            # These are profile names, not raw provider options or model identifiers.
            # Independent helper histories do not borrow the main agent's tool state.
            for profile in ("json", "text"):
                async for event in provider.stream(
                    ModelRequest([UserMessage(label)], options={"sampling_profile": profile}),
                    CancelToken(),
                ):
                    text = event.message.content[0].text
                    extra[profile] = json.loads(text) if profile == "json" else text
    answer = await ctx.session.elicit_form(
        f"Choose output for {label}",
        FORM,
        related_request_id=ctx.request_context.request_id,
    )
    return json.dumps(
        {
            "label": label,
            "model_reply": result.messages[-1].content[0].text,
            "action": answer.action,
            "selection": answer.content,
            "auxiliary": extra,
        }
    )


if __name__ == "__main__":
    if sys.argv[1:2] == ["--http"]:
        server.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        server.run()
