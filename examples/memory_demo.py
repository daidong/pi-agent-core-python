"""Long-term memory with a plugin: a fact saved in one conversation is used in the next.

The plugin in examples/plugins/memory keeps facts in a JSON file and puts them in the
system prompt of every request. Each conversation below opens its own plugin set, as a
new process would. The model is scripted so this runs offline.

    python examples/memory_demo.py
"""

import asyncio
import json
import tempfile
from pathlib import Path

from pi_python import AssistantMessage, ScriptedProvider, ToolCall
from pi_python.plugins import load_plugins

PLUGIN = Path(__file__).parent / "plugins" / "memory"


def call(tool: str, **args) -> AssistantMessage:
    return AssistantMessage([ToolCall(f"call-{tool}", tool, args)], "tool_use")


async def conversation(provider: ScriptedProvider, options: dict, text: str) -> None:
    # The plugin merges facts with a model call when its list is full; it gets the
    # Provider as a service, so the application decides which models plugins may use.
    async with load_plugins([PLUGIN], services={"provider": provider}, options=options) as plugins:
        agent = plugins.agent(provider=provider, system_prompt="You are a project assistant.")
        result = await agent.prompt(text)
        assert result.status == "completed", result.errors


async def main() -> None:
    path = Path(tempfile.mkdtemp()) / "memory.json"
    options = {"memory": {"path": str(path), "max_facts": 2}}

    first = ScriptedProvider(
        [call("remember", fact="The paper deadline is Friday."), AssistantMessage.text("Noted.")]
    )
    await conversation(first, options, "Remember: the paper deadline is Friday.")
    print("saved:", json.loads(path.read_text(encoding="utf-8"))["facts"])

    second = ScriptedProvider(
        [
            call("remember", fact="The paper deadline moved to Monday."),
            call("remember", fact="Use the ACM template."),
            # The list would hold 3 facts, more than max_facts, so the plugin asks the
            # model to merge them; this reply is that merge.
            AssistantMessage.text("The paper deadline is Monday.\nUse the ACM template."),
            AssistantMessage.text("Noted: Monday, ACM template."),
        ]
    )
    await conversation(second, options, "The deadline moved to Monday. Also use the ACM template.")
    system = second.requests[0].messages[0]
    print("memory in the next conversation's system prompt:")
    print("  " + system.sections["memory"].replace("\n", "\n  "))
    print("after merging:", json.loads(path.read_text(encoding="utf-8"))["facts"])


if __name__ == "__main__":
    asyncio.run(main())
