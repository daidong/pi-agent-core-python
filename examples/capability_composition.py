"""Offline C29 example: application-owned knowledge, tools, hooks and validation.

This toy check demonstrates composition, not the effectiveness of domain knowledge.
Run with: python examples/capability_composition.py
"""

import asyncio
import json
from dataclasses import dataclass, field

from pi_python import (
    Agent,
    AssistantMessage,
    ConfigurationError,
    CustomMessage,
    Hooks,
    ScriptedProvider,
    SystemMessage,
    Tool,
    ToolCall,
    ToolInvocation,
    ToolResult,
    UserMessage,
)


@dataclass
class CheckPack:
    """A plain Python object owned by one application session, not a plugin loader."""

    name: str = "sample-checks"
    version: str = "1"
    minimum: int = 0
    calls: list[int] = field(default_factory=list)

    @property
    def instructions(self):
        return {
            self.name: "Check the sample before interpreting it. A normal run is not validation."
        }

    def before(self, call, args, context):
        # Applications explicitly combine vetoes; a later allow cannot erase a block.
        return args.get("value") != 999

    async def check(self, args, context):
        value = args["value"]
        self.calls.append(value)
        passed = value >= self.minimum
        return ToolResult.text(
            f"sample={value}; domain_passed={passed}",
            structured_content={"domain_passed": passed},
        )

    def tools(self):
        return [
            Tool(
                "check_sample",
                "Check a sample against the application rule",
                {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                self.check,
            )
        ]


def compose(packs, provider, mode):
    """Resolve instruction names and hook order here; Agent checks tool-name conflicts."""
    sections, tools, metadata = {}, [], []
    for pack in packs:
        for name, text in pack.instructions.items():
            if name in sections:
                raise ConfigurationError(f"Duplicate instruction section: {name}")
            sections[name] = text
        tools.extend(pack.tools())
        metadata.append({"name": pack.name, "version": pack.version, "minimum": pack.minimum})

    def before(call, args, context):
        for pack in packs:  # explicit order, stop at the first veto
            if pack.before(call, args, context) is False:
                return False
        return True

    def convert(messages):
        return [
            UserMessage("Application capability configuration: " + json.dumps(m.data))
            if isinstance(m, CustomMessage) and m.custom_type == "capability_configuration"
            else m  # core handles application_tool_batch, not arbitrary custom types
            for m in messages
        ]

    return Agent(
        provider=provider,
        tools=tools,
        messages=[
            SystemMessage(sections=sections),
            CustomMessage("capability_configuration", {"packs": metadata, "mode": mode}),
        ],
        hooks=Hooks(before_tool_call=before, convert_to_llm=convert),
    )


async def compare():
    model_pack, application_pack = CheckPack(), CheckPack()
    model_provider = ScriptedProvider(
        [
            AssistantMessage([ToolCall("model-check", "check_sample", {"value": -1})], "tool_use"),
            AssistantMessage.text("The check failed."),
        ]
    )
    application_provider = ScriptedProvider([AssistantMessage.text("The fixed check failed.")])
    async with compose([model_pack], model_provider, "model") as model_agent:
        model_result = await model_agent.prompt("Check sample -1 and interpret it.")
    async with compose([application_pack], application_provider, "application") as app_agent:
        batch = await app_agent.execute_tools(
            [ToolInvocation("check_sample", {"value": -1})], source="sample-checks@1"
        )
        assert not application_provider.requests
        # Domain validation uses the independent tool result, not run status or model prose.
        domain_passed = batch.tool_outcomes[0].result.structured_content["domain_passed"]
        interpretation = await app_agent.prompt(
            "Interpret the fixed check; do not claim it passed."
        )
    assert model_result.status == batch.status == interpretation.status == "completed"
    assert domain_passed is False
    assert model_pack.calls == application_pack.calls == [-1]
    assert model_pack.calls is not application_pack.calls
    assert CheckPack().calls == []
    return {
        "model": model_result,
        "application": batch,
        "domain_passed": domain_passed,
        "model_requests": len(model_provider.requests),
        "application_requests": len(application_provider.requests),
    }


async def main():
    result = await compare()
    print(f"run={result['application'].status}; domain_passed={result['domain_passed']}")
    print("Both paths use the same tool; the application batch made zero model requests.")


if __name__ == "__main__":
    asyncio.run(main())
