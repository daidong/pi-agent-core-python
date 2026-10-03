"""Five-minute start: tools from plain functions, one blocking call, streamed output.

python examples/quickstart.py                 # offline, with a scripted model
ANTHROPIC_API_KEY=... python examples/quickstart.py --model claude-sonnet-4-5
"""

import argparse
import os
from datetime import date

from pi_python import Agent, AssistantMessage, ScriptedProvider, ToolCall, tool


@tool
def days_until(target: date) -> int:
    """Count the days from today until a date.

    Args:
        target: The date to count to, for example 2026-12-25.
    """
    return (target - date.today()).days


@tool
async def convert(value: float, unit: str = "km") -> str:
    """Convert a distance between kilometers and miles.

    Args:
        value: The distance.
        unit: "km" to convert kilometers to miles, "mi" for the reverse.
    """
    return f"{value * 0.621371:.1f} mi" if unit == "km" else f"{value / 0.621371:.1f} km"


def provider(model: str | None):
    if model:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("--model needs ANTHROPIC_API_KEY; omit --model to run offline")
        from pi_python.providers import AnthropicProvider

        return AnthropicProvider(api_key=os.environ["ANTHROPIC_API_KEY"])
    # Offline: a scripted model that calls both tools, then answers.
    calls = [
        ToolCall("c1", "days_until", {"target": "2026-12-25"}),
        ToolCall("c2", "convert", {"value": 42.195}),
    ]
    return ScriptedProvider(
        [
            AssistantMessage(calls, "tool_use"),
            AssistantMessage.text("Christmas is coming, and a marathon is about 26.2 miles."),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="A Claude model id; runs offline without it")
    args = parser.parse_args()
    agent = Agent(
        provider=provider(args.model),
        model=args.model or "mock",
        system_prompt="Answer briefly. Use the tools for dates and conversions.",
        tools=[days_until, convert],
    )

    def show(event):
        if event.type == "message_update" and event.data["delta_type"] == "text_delta":
            print(event.data["delta"], end="", flush=True)
        elif event.type == "tool_execution_end":
            blocks = event.data["result"]["content"]
            print(f"[{event.call_id}]", " ".join(b.get("text", "[image]") for b in blocks))

    agent.subscribe(show)
    result = agent.prompt_sync(
        "How many days until Christmas 2026, and how long is a marathon in miles?"
    )
    print(f"\nstatus={result.status}; answer: {result.messages[-1].content[0].text}")


if __name__ == "__main__":
    main()
