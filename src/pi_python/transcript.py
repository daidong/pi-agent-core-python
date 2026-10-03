"""System replay adapted from Pi a13d35a (MIT); see NOTICE."""

from copy import deepcopy
from .messages import Message, SystemMessage, TextContent, ToolDeclaration


def system_text(content: str | list[TextContent]) -> str:
    """Pi's contentText: text blocks of one message joined by a single newline."""
    return content if isinstance(content, str) else "\n".join(b.text for b in content)


def system_message_text(message: SystemMessage) -> str:
    """A complete prompt: content followed by the sections that are set."""
    parts = [system_text(message.content), *(v for v in message.sections.values() if v is not None)]
    return "\n\n".join(p for p in parts if p)


def render_system_update(message: SystemMessage) -> str:
    """A later system message as sent in place; section changes are framed by name."""
    parts = [system_text(message.content)] if system_text(message.content) else []
    for name, value in message.sections.items():
        parts.append(
            f'Removed system prompt section "{name}".'
            if value is None
            else f'Updated system prompt section "{name}":\n\n{value}'
        )
    return "\n\n".join(parts)


def current_tools(messages: list[Message]) -> list[ToolDeclaration]:
    tools: dict[str, ToolDeclaration] = {}
    for m in messages:
        if isinstance(m, SystemMessage):
            for name in m.tools_removed:
                tools.pop(name, None)
            for tool in m.tools_added:
                tools[tool.name] = tool
    return deepcopy(list(tools.values()))


def current_system_message(messages: list[Message]) -> SystemMessage | None:
    systems = [m for m in messages if isinstance(m, SystemMessage)]
    if not systems:
        return None
    sections: dict[str, str | None] = {}
    for m in systems:
        for name, text in m.sections.items():
            if text is None:
                sections.pop(name, None)
            else:
                sections[name] = text
    return SystemMessage(
        "\n\n".join(system_text(m.content) for m in systems if system_text(m.content)),
        sections,
        current_tools(messages),
        timestamp=systems[0].timestamp,
    )


def current_system_prompt(messages: list[Message]) -> str:
    m = current_system_message(messages)
    return system_message_text(m) if m else ""


def initial_system_message(messages: list[Message]) -> SystemMessage | None:
    return messages[0] if messages and isinstance(messages[0], SystemMessage) else None


def collapse_system_messages(messages: list[Message]) -> list[Message]:
    """For APIs without mid-conversation system messages: one replayed leading message."""
    head = current_system_message(messages)
    rest: list[Message] = [deepcopy(m) for m in messages if not isinstance(m, SystemMessage)]
    return [head, *rest] if head else rest


def resolve_transcript(messages: list[Message], mid_conversation: bool) -> list[Message]:
    return deepcopy(messages) if mid_conversation else collapse_system_messages(messages)


def declared_tools(messages: list[Message]) -> list[ToolDeclaration]:
    """Every definition referenced by transcript tool state, in first-declaration order."""
    tools: dict[str, ToolDeclaration] = {}
    for m in messages:
        if isinstance(m, SystemMessage):
            for tool in m.tools_added:
                tools[tool.name] = tool
    return deepcopy(list(tools.values()))


def has_tool_redefinitions(messages: list[Message]) -> bool:
    """A name declared twice with different definitions cannot be referenced by name."""
    seen: dict[str, ToolDeclaration] = {}
    for m in messages:
        if isinstance(m, SystemMessage):
            for tool in m.tools_added:
                if tool.name in seen and seen[tool.name] != tool:
                    return True
                seen[tool.name] = tool
    return False


def has_non_additive_tool_changes(messages: list[Message]) -> bool:
    """A removal or same-name redeclaration that an addition-only transport cannot replay."""
    seen: set[str] = set()
    for m in messages:
        if isinstance(m, SystemMessage):
            if m.tools_removed:
                return True
            for tool in m.tools_added:
                if tool.name in seen:
                    return True
                seen.add(tool.name)
    return False


def resolve_transcript_tools(
    messages: list[Message], supports_additions: bool
) -> tuple[list[ToolDeclaration], bool]:
    """Top-level request tools, and whether later additions are anchored in place."""
    anchors = supports_additions and not has_non_additive_tool_changes(messages)
    initial = initial_system_message(messages)
    tools = (
        (deepcopy(initial.tools_added) if initial else []) if anchors else current_tools(messages)
    )
    return tools, anchors


def with_request_tools(messages: list[Message], tools: list[ToolDeclaration]) -> list[Message]:
    """Pi's normalizeContext: fold request-level tools into the leading system message.

    Requests built by Agent already carry their tools in the transcript; this covers a
    direct Provider call that passes `tools` without declaring them in system messages.
    """
    if not tools or any(isinstance(m, SystemMessage) and m.tools_added for m in messages):
        return deepcopy(messages)
    messages = deepcopy(messages)
    head = initial_system_message(messages)
    if head is None:
        return [SystemMessage(tools_added=deepcopy(tools), timestamp=0), *messages]
    head.tools_added = deepcopy(tools)
    return messages


def declare_tool_changes(
    history: list[Message], pending: list[Message], tools: list[ToolDeclaration]
) -> list[Message]:
    pending = deepcopy(pending)
    systems = [i for i, m in enumerate(pending) if isinstance(m, SystemMessage)]
    index = systems[-1] if systems else None
    if index is not None:
        m = pending[index]
        assert isinstance(m, SystemMessage)
        m.tools_added, m.tools_removed = [], []
    previous = {t.name: t for t in current_tools(history + pending)}
    current = {t.name: t for t in tools}
    added = [t for t in tools if previous.get(t.name) != t]
    removed = [name for name, t in previous.items() if current.get(name) != t]
    if index is not None:
        m = pending[index]
        assert isinstance(m, SystemMessage)
        m.tools_added, m.tools_removed = deepcopy(added), removed
    elif added or removed:
        index = next(
            (i for i, m in enumerate(pending) if not isinstance(m, SystemMessage)), len(pending)
        )
        pending.insert(index, SystemMessage(tools_added=deepcopy(added), tools_removed=removed))
    return pending
