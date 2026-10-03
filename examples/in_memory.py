"""Run with: uv run python examples/in_memory.py (no network/model credentials)."""

import asyncio
from pi_python import Agent, AssistantMessage, ScriptedProvider, Tool, ToolCall, ToolResult


async def inspect_log(args, context):
    return ToolResult.text(f"{args['name']}: 3 records, 0 missing")


async def main():
    provider = ScriptedProvider(
        [
            AssistantMessage(
                [ToolCall("inspect-1", "inspect_log", {"name": "telemetry"})], "tool_use"
            ),
            AssistantMessage.text("The telemetry contains 3 complete records."),
        ]
    )
    async with Agent(
        provider=provider,
        system_prompt="Inspect telemetry.",
        tools=[
            Tool(
                "inspect_log",
                "Inspect an in-memory sample",
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                inspect_log,
            )
        ],
    ) as agent:
        result = await agent.prompt("Check the sample.")
        assert result.status == "completed", result
        assert len(provider.requests) == 2
        print(result.messages[-1].content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
