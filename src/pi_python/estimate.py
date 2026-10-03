"""Context-size estimate ported from Pi a13d35a utils/estimate.ts (MIT); see NOTICE.

A character heuristic, not a tokenizer: four UTF-16 code units per token and a fixed
charge per image. It sizes output limits and compaction decisions; the service still
enforces the real context limit.
"""

from __future__ import annotations
from typing import Any
import json
import math
from .messages import (
    AssistantMessage,
    ImageContent,
    Message,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolResultMessage,
    UserMessage,
)
from .transcript import system_message_text

_CHARS_PER_TOKEN = 4
_IMAGE_CHARS = 4800
_SAFETY_TOKENS = 4096


def _length(text: str) -> int:
    """JavaScript string length: UTF-16 code units."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tokens(chars: int) -> int:
    return math.ceil(chars / _CHARS_PER_TOKEN)


def _content_tokens(content: str | list[TextContent | ImageContent]) -> int:
    if isinstance(content, str):
        return _tokens(_length(content))
    return _tokens(
        sum(_length(b.text) if isinstance(b, TextContent) else _IMAGE_CHARS for b in content)
    )


def estimate_message_tokens(message: Message) -> int:
    if isinstance(message, SystemMessage):
        tokens = _tokens(_length(system_message_text(message)))
        if message.tools_added:
            tools = [
                {"name": t.name, "description": t.description, "parameters": t.input_schema}
                for t in message.tools_added
            ]
            tokens += _tokens(_length(_json(tools)))
        if message.tools_removed:
            tokens += _tokens(_length(_json([{"name": n} for n in message.tools_removed])))
        return tokens
    if isinstance(message, (UserMessage, ToolResultMessage)):
        return _content_tokens(message.content)
    if isinstance(message, AssistantMessage):
        chars = 0
        for block in message.content:
            if isinstance(block, TextContent):
                chars += _length(block.text)
            elif isinstance(block, ThinkingContent):
                chars += _length(block.thinking)
            elif not isinstance(block, ImageContent):
                chars += _length(block.name) + _length(_json(block.arguments))
        return _tokens(chars)
    return 0


def _usage_tokens(usage: dict) -> int:
    total = usage.get("total_tokens") or 0
    return total or sum(
        usage.get(k, 0) or 0 for k in ("input", "output", "cache_read", "cache_write")
    )


def estimate_context_tokens(messages: list[Message]) -> int:
    """Last valid reported usage plus an estimate of everything after it."""
    latest = -math.inf
    anchor = None
    for index, message in enumerate(messages):
        if isinstance(message, AssistantMessage):
            # A newer prefix message inserted after this response invalidates its usage.
            if (
                message.timestamp >= latest
                and message.stop_reason not in {"aborted", "error"}
                and _usage_tokens(message.usage) > 0
            ):
                anchor = index
        latest = max(latest, message.timestamp)
    if anchor is None:
        return sum(estimate_message_tokens(m) for m in messages)
    message = messages[anchor]
    assert isinstance(message, AssistantMessage)
    return _usage_tokens(message.usage) + sum(
        estimate_message_tokens(m) for m in messages[anchor + 1 :]
    )


def clamp_max_tokens_to_context(
    context_window: int, messages: list[Message], max_tokens: int
) -> int:
    """Pi clampMaxTokensToContext: leave room for the estimated prompt and a margin."""
    if context_window <= 0:
        return max(1, max_tokens)
    available = context_window - estimate_context_tokens(messages) - _SAFETY_TOKENS
    return min(max_tokens, max(1, available))


def short_hash(text: str) -> str:
    """Pi utils/hash.ts shortHash, bit for bit (32-bit Math.imul over UTF-16 units)."""

    def imul(a: int, b: int) -> int:
        result = (a * b) & 0xFFFFFFFF
        return result - 0x100000000 if result & 0x80000000 else result

    def u32(value: int) -> int:
        return value & 0xFFFFFFFF

    def base36(value: int) -> str:
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        out = ""
        while True:
            value, rest = divmod(value, 36)
            out = digits[rest] + out
            if not value:
                return out

    h1, h2 = 0xDEADBEEF, 0x41C6CE57
    units = text.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(units), 2):
        ch = units[i] | units[i + 1] << 8
        h1 = imul(u32(h1) ^ ch, 2654435761)
        h2 = imul(u32(h2) ^ ch, 1597334677)
    h1 = imul(u32(h1) ^ (u32(h1) >> 16), 2246822507) ^ imul(u32(h2) ^ (u32(h2) >> 13), 3266489909)
    h2 = imul(u32(h2) ^ (u32(h2) >> 16), 2246822507) ^ imul(u32(h1) ^ (u32(h1) >> 13), 3266489909)
    return base36(u32(h2)) + base36(u32(h1))
