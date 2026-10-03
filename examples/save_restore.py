"""Save a conversation to a file and continue it later, in another process.

The core never writes files; `encode_messages` and `decode_messages` turn the history
into JSON and back, validating it. The application decides where and when to store it.

    python examples/save_restore.py
"""

import json
import os
import tempfile
from pathlib import Path

from pi_python import (
    Agent,
    AssistantMessage,
    ScriptedProvider,
    decode_messages,
    encode_messages,
)


def save(path: Path, agent: Agent) -> None:
    """Write atomically, so a crash never leaves half a file."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(encode_messages(list(agent.state.messages)))
    os.replace(temporary, path)


def load(path: Path):
    return decode_messages(path.read_text(encoding="utf-8")) if path.exists() else []


def main() -> None:
    path = Path(tempfile.mkdtemp()) / "conversation.json"

    first = Agent(
        provider=ScriptedProvider([AssistantMessage.text("Noted: the deadline is Friday.")]),
        system_prompt="You are a project assistant.",
    )
    first.prompt_sync("Remember: the paper deadline is Friday.")
    save(path, first)
    print(f"saved {len(first.state.messages)} messages to {path.name}")

    # Later: the system prompt and tool declarations are part of the saved history.
    second = Agent(
        provider=ScriptedProvider([AssistantMessage.text("The deadline is Friday.")]),
        messages=load(path),
    )
    result = second.prompt_sync("When is the deadline?")
    print("answer after restore:", result.messages[-1].content[0].text)
    print("roles:", [m.role for m in second.state.messages])
    print("schema version:", json.loads(path.read_text())["schema_version"])


if __name__ == "__main__":
    main()
