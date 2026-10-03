"""Recover from a full context window and from transient provider errors.

A failed response stays in the history as an assistant message with stop_reason "error".
`is_context_overflow` and `is_retryable_error` read it; `continue_run()` retries it. To
shrink the context, `transform_context` replaces older turns with a summary in every
request while the stored history stays complete.

    python examples/recovery.py
"""

import asyncio

from pi_python import (
    Agent,
    AssistantMessage,
    Hooks,
    Message,
    ModelEvent,
    RunResult,
    SystemMessage,
    UserMessage,
    is_context_overflow,
    is_retryable_error,
    retry_delay,
)


class Compactor:
    """Summarize all but the last few turns; cut only before a user message, so a tool
    call and its result are never separated."""

    def __init__(self, summarize, keep_turns: int = 1):
        self.summarize = summarize
        self.keep_turns = keep_turns
        self.cut = 0
        self.summary: str | None = None

    async def compact(self, messages: list[Message]) -> bool:
        turns = [i for i, m in enumerate(messages) if isinstance(m, UserMessage)]
        if len(turns) <= self.keep_turns or turns[-self.keep_turns] <= self.cut:
            return False  # nothing older left to summarize
        cut = turns[-self.keep_turns]
        older = [m for m in messages[self.cut : cut] if not isinstance(m, SystemMessage)]
        if self.summary:
            older.insert(0, UserMessage(f"Earlier summary: {self.summary}"))
        self.summary, self.cut = await self.summarize(older), cut
        return True

    def transform_context(self, messages: list[Message], cancel) -> list[Message]:
        if self.summary is None:
            return messages
        # Keep system messages: they carry the instructions and tool declarations.
        kept = [m for m in messages[: self.cut] if isinstance(m, SystemMessage)]
        note = UserMessage(f"Summary of the earlier conversation: {self.summary}")
        return [*kept, note, *messages[self.cut :]]


async def prompt_with_recovery(
    agent: Agent, text: str, compactor: Compactor, context_window: int, attempts: int = 3
) -> RunResult:
    result = await agent.prompt(text)
    for attempt in range(1, attempts + 1):
        failed = result.messages[-1] if result.messages else None
        if not isinstance(failed, AssistantMessage):
            break
        if is_context_overflow(failed, context_window):
            if not await compactor.compact(list(agent.state.messages)):
                break
            print(f"context full: compacted the first {compactor.cut} messages, retrying")
        elif is_retryable_error(failed):
            delay = retry_delay(attempt, base=0.1)  # use the default base (2 s) for real services
            print(f"transient error ({failed.error}); retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
        else:
            break
        result = await agent.continue_run()
    return result


class DemoModel:
    """Offline stand-in: overflows once the history is long, fails once with a 503."""

    def __init__(self):
        self.calls = 0

    async def stream(self, request, cancel):
        self.calls += 1
        if self.calls == 3:
            raise RuntimeError("503 service unavailable")
        if len(request.messages) > 5:
            raise RuntimeError("prompt is too long: 210000 tokens > 200000 maximum")
        yield ModelEvent.done(
            AssistantMessage.text(f"answer {self.calls} from {len(request.messages)} messages")
        )


async def main() -> None:
    async def summarize(messages: list[Message]) -> str:
        # In practice: ask a model for a structured summary (goal, progress, decisions, next steps).
        return f"{len(messages)} earlier messages about planning a trip to Paris"

    compactor = Compactor(summarize)
    agent = Agent(
        provider=DemoModel(),
        system_prompt="You help plan trips.",
        hooks=Hooks(transform_context=compactor.transform_context),
    )
    for question in ["Plan a day in Paris.", "Add a museum.", "Where should we eat?"]:
        result = await prompt_with_recovery(agent, question, compactor, context_window=200_000)
        print(f"{question!r} -> {result.status}: {result.messages[-1].content[0].text}")
    print("stored history keeps every message:", len(agent.state.messages))


if __name__ == "__main__":
    asyncio.run(main())
