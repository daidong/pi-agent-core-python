"""Both entry points are included in one package; this example needs no network."""

import asyncio

from pi_python import Agent, AssistantMessage, ScriptedProvider, ToolCall, ToolResult, tool
from pi_python import core


@tool
def double(value: int) -> int:
    """Double a research value."""
    return value * 2


async def main() -> None:
    research = Agent(
        provider=ScriptedProvider(
            [
                AssistantMessage([ToolCall("c", "double", {"value": 3})], "tool_use"),
                AssistantMessage.text("6"),
            ]
        ),
        tools=[double],
    )
    result = await research.prompt("Double 3")
    assert result.status == "completed"
    print("Research result:", result.messages[-1].content[0].text)

    async def execute(call_id, args, signal, on_update):
        await on_update(ToolResult.text("Computing"))
        return ToolResult.text(str(args["value"] * 2))

    agent = core.Agent(
        provider=ScriptedProvider(
            [
                AssistantMessage([ToolCall("c", "double", {"value": 3})], "tool_use"),
            ]
        ),
        tools=[core.Tool("double", "Double a value", {"type": "object"}, execute)],
        finish_turn=lambda context, signal: {"action": "end"},
    )
    seen = []
    agent.subscribe(lambda event, signal: seen.append(event))
    await agent.prompt("Double 3")
    assert agent.state.error_message is None
    assert seen[-1]["type"] == "agent_end"
    print("Core tool result:", seen[-1]["messages"][-1]["content"][0]["text"])
    await research.aclose()
    await agent.aclose()


if __name__ == "__main__":
    asyncio.run(main())
