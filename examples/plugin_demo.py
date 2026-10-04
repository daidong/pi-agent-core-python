"""Load a local plugin and run an agent with everything it contributes.

The plugin in examples/plugins/lab_tools adds a tool, an instruction, a safety hook, a
skill, a prompt template and a reviewer subagent. The model is scripted so this runs
offline; pass a real provider and model to `plugins.agent(...)` to use it for real.

    python examples/plugin_demo.py
"""

import asyncio
from pathlib import Path

from pi_python import AssistantMessage, ScriptedProvider, ToolCall
from pi_python.plugins import load_plugins

PLUGIN = Path(__file__).parent / "plugins" / "lab_tools"


def call(tool: str, **args) -> AssistantMessage:
    return AssistantMessage([ToolCall(f"call-{tool}", tool, args)], "tool_use")


async def main() -> None:
    model = ScriptedProvider(
        [
            call("read_skill", name="dedup-window"),  # the main agent reads the skill
            call("subagent", agent="reviewer", task="Count duplicates in a,b,a,c,a"),
            call("count_duplicates", rows=["a", "b", "a", "c", "a"]),  # the reviewer works
            AssistantMessage.text("2"),  # the reviewer's answer
            AssistantMessage.text("The reviewer found 2 duplicate rows."),
        ]
    )
    async with load_plugins([PLUGIN], options={"lab_tools": {"lab": "the HPC lab"}}) as plugins:
        print("loaded:", ", ".join(f"{p.name} {p.version}" for p in plugins.plugins))
        for check in await plugins.check():
            print("check:", check.name, "passed" if check.passed else check.detail)
        agent = plugins.agent(provider=model, system_prompt="You help analyze event logs.")
        prompt = plugins.expand("/dedup events.csv 30")
        print("prompt:", prompt)
        result = await agent.prompt(prompt)
        print("tools used:", [o.call.name for o in result.tool_outcomes])
        print("answer:", result.messages[-1].content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
