"""An example plugin: facts saved in one conversation are known in the next.

The facts live in the JSON file named by the `path` option. The model saves a fact with
`remember` and removes a wrong or outdated one with `forget`. Before every model request,
the facts become the "memory" section of the system prompt; the stored conversation is
not changed. The section changes only when the facts do, so the cached prompt prefix
stays valid between changes.

When a new fact would make the list longer than `max_facts` (default 50), `remember`
asks a model to merge the list. That call uses the Provider the application passes as
the "provider" service, with the model named by the `model` option, or else the calling
agent's model. Without a provider, or when merging does not shorten the list, the oldest
facts are dropped.

The plugin does not decide what is worth remembering beyond what the model saves, and
it assumes one process writes the file at a time. Adapt both to your application.
"""

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from pi_python import Agent, ConfigurationError, SystemMessage, ToolContext

__version__ = "0.1.0"

MERGE_PROMPT = (
    "You maintain a list of saved facts. Merge facts that say the same thing and drop"
    " facts that a later fact makes outdated. Reply with the remaining facts, one per"
    " line, and nothing else."
)


def setup(api):
    if "path" not in api.options:
        raise ConfigurationError(
            f"The memory plugin needs a file: options={{{api.name!r}: {{'path': ...}}}}"
        )
    path = Path(api.options["path"]).expanduser()
    max_facts = api.options.get("max_facts", 50)
    if type(max_facts) is not int or max_facts < 1:
        raise ConfigurationError("max_facts must be a positive integer")
    provider = api.service("provider", default=None)
    facts: list[str] = load(path)
    lock = asyncio.Lock()  # tool calls can run in parallel

    async def shorten(updated: list[str], context: ToolContext) -> list[str]:
        if provider is not None:
            merger = Agent(
                provider=provider,
                model=api.options.get("model") or context.agent_context.model,
                system_prompt=MERGE_PROMPT,
            )
            # run_agent ties the call to this tool call: aborting the run aborts it.
            result = await context.run_agent(merger, "\n".join(updated))
            reply = "".join(getattr(b, "text", "") for b in result.messages[-1].content)
            merged = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line) for line in reply.splitlines()]
            merged = [line for line in merged if line]
            if result.status == "completed" and 0 < len(merged) < len(updated):
                updated = merged
        return updated[-max_facts:]

    async def remember(fact: str, context: ToolContext) -> str:
        """Save a fact that should still be known in later conversations.

        Args:
            fact: One short, self-contained statement, such as "The paper deadline is Friday".
        """
        fact = " ".join(fact.split())
        if not fact:
            return "Nothing saved: the fact is empty."
        async with lock:
            if fact in facts:
                return f"Already saved as fact {facts.index(fact) + 1}."
            updated = [*facts, fact]
            if len(updated) > max_facts:
                updated = await shorten(updated, context)
            save(path, updated)
            facts[:] = updated
        return f"Saved. Facts in memory: {len(facts)}."

    async def forget(number: int) -> str:
        """Remove a saved fact that is wrong or outdated.

        Args:
            number: The fact's number in the memory section of the system prompt.
        """
        async with lock:
            if not 1 <= number <= len(facts):
                return f"There is no fact {number}. Facts in memory: {len(facts)}."
            removed = facts[number - 1]
            updated = facts[: number - 1] + facts[number:]
            save(path, updated)  # change the list only after the file is written
            facts[:] = updated
        return f"Removed: {removed}"

    api.add_tool(remember)
    api.add_tool(forget)
    api.add_system_prompt(
        "When the user states a fact or preference that should be known in later"
        " conversations, save it with remember. Remove saved facts that turn out wrong"
        " with forget."
    )

    @api.on("transform_context")
    def show_memory(messages, cancel):
        # The messages are a copy for this request; changing them changes only what the
        # model receives. Agents from a plugin set have a leading system message.
        if not facts or not messages or not isinstance(messages[0], SystemMessage):
            return None
        listed = "\n".join(f"{i}. {fact}" for i, fact in enumerate(facts, 1))
        messages[0].sections["memory"] = (
            "Facts saved in earlier conversations, numbered for forget:\n" + listed
        )
        return messages

    api.add_check(lambda: path.parent.is_dir(), "directory_exists")


def load(path: Path) -> list[str]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    facts = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(facts, list) or not all(isinstance(f, str) for f in facts):
        raise ConfigurationError(f"{path}: facts must be a list of strings")
    return facts


def save(path: Path, facts: list[str]) -> None:
    """Write atomically, so a crash never leaves half a file."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump({"facts": facts}, file, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
