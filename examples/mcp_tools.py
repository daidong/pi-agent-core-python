"""Give an agent the tools of an MCP server (needs `pip install 'pi-python-core[mcp]'`).

    python examples/mcp_tools.py                                   # bundled demo server
    python examples/mcp_tools.py -- uvx mcp-server-fetch           # any stdio MCP server

The offline scripted model calls the demo server's `add` tool; with another server it
only lists the tools. Swap in a real provider to let a model choose among them.
"""

import asyncio
import sys
from pathlib import Path

from pi_python import Agent, AssistantMessage, ScriptedProvider, ToolCall
from pi_python.mcp import connect_stdio

DEMO = [
    sys.executable,
    str(Path(__file__).resolve().parents[1] / "tests" / "servers" / "mcp_server.py"),
]


async def main(command: list[str]) -> None:
    async with connect_stdio(command[0], command[1:], prefix="mcp") as tools:
        for tool in tools:
            print(f"{tool.name}: {tool.description.splitlines()[0]}")
        if command != DEMO:
            return
        model = ScriptedProvider(
            [
                AssistantMessage([ToolCall("c1", "mcp_add", {"a": 19, "b": 23})], "tool_use"),
                AssistantMessage.text("19 + 23 = 42"),
            ]
        )
        agent = Agent(provider=model, tools=tools)
        result = await agent.prompt("What is 19 + 23?")
        print("tool result:", result.tool_outcomes[0].result.content[0].text)
        print("answer:", result.messages[-1].content[0].text)


if __name__ == "__main__":
    argv = sys.argv[1:]
    asyncio.run(main(argv[argv.index("--") + 1 :] if "--" in argv else DEMO))
