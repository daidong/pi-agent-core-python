"""A sub-agent as a tool: the main agent delegates a focused task to a second Agent.

The inner agent has its own model, instructions and tools, and its own history, so the
main conversation only sees the result. Cancelling the outer call aborts the inner run.

    python examples/subagent.py
"""

import asyncio

from pi_python import (
    Agent,
    AssistantMessage,
    ScriptedProvider,
    ToolCall,
    ToolContext,
    ToolResult,
    tool,
)


@tool
def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())


def researcher() -> Agent:
    """A fresh specialist per call; swap in a real provider and model here."""
    model = ScriptedProvider(
        [
            AssistantMessage(
                [ToolCall("w", "word_count", {"text": "to be or not to be"})], "tool_use"
            ),
            AssistantMessage.text("The line has 6 words; 'to' and 'be' each appear twice."),
        ]
    )
    return Agent(provider=model, system_prompt="You analyze text precisely.", tools=[word_count])


@tool
async def ask_researcher(question: str, context: ToolContext) -> ToolResult:
    """Delegate a text-analysis question to a specialist agent.

    Args:
        question: The question, with the text to analyze.
    """
    inner = researcher()
    # Forward the outer cancellation to the inner run.
    watcher = asyncio.create_task(context.cancel.wait())
    watcher.add_done_callback(lambda _: inner.abort("parent cancelled"))
    try:
        result = await inner.prompt(question)
    finally:
        watcher.cancel()
    answer = result.messages[-1]
    text = "".join(getattr(block, "text", "") for block in answer.content)
    return ToolResult.text(
        text or f"Specialist stopped: {result.status}", is_error=result.status != "completed"
    )


async def main() -> None:
    model = ScriptedProvider(
        [
            AssistantMessage(
                [ToolCall("r", "ask_researcher", {"question": "Analyze: 'to be or not to be'"})],
                "tool_use",
            ),
            AssistantMessage.text("The specialist reports 6 words, with 'to' and 'be' repeated."),
        ]
    )
    agent = Agent(provider=model, tools=[ask_researcher])
    result = await agent.prompt("What can you tell me about 'to be or not to be'?")
    print("specialist said:", result.tool_outcomes[0].result.content[0].text)
    print("final answer:   ", result.messages[-1].content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
