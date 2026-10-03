"""Recognize failed responses an application can recover from.

Ported from pi-ai (utils/overflow.ts, utils/retry.ts at the pinned Pi revision). The
functions read a committed assistant message; the application decides what to do, for
example compact the history and continue after an overflow, or back off and continue
after a transient error. `Agent.continue_run()` retries a failed or aborted last response.
"""

from __future__ import annotations

import re

from .messages import AssistantMessage

# Context overflow messages, per provider (see the pinned overflow.ts for examples).
_OVERFLOW = [
    re.compile(pattern, re.I)
    for pattern in (
        r"prompt (?:is )?too long",  # Anthropic, z.ai
        r"prompt exceeds max length",  # z.ai CN
        r"request_too_large",  # Anthropic byte-size overflow (HTTP 413)
        r"input is too long for requested model",  # Amazon Bedrock
        r"exceeds the context window",  # OpenAI Completions and Responses
        r"exceeds (?:the )?(?:model'?s )?maximum context length(?: of [\d,]+ tokens?|\s*\([\d,]+\))",
        r"input token count.*exceeds the maximum",  # Google Gemini
        r"maximum prompt length is \d+",  # xAI
        r"reduce the length of the messages",  # Groq
        r"maximum context length is \d+ tokens",  # OpenRouter
        r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?",  # OpenRouter/Poolside
        r"input \(\d+ tokens\) is longer than the model'?s context length \(\d+ tokens\)",  # Together
        r"exceeds the limit of \d+",  # GitHub Copilot
        r"exceeds the available context size",  # llama.cpp server
        r"greater than the context length",  # LM Studio
        r"context window exceeds limit",  # MiniMax
        r"exceeded model token limit",  # Kimi For Coding
        r"too large for model with \d+ maximum context length",  # Mistral
        r"prompt has [\d,]+ tokens?, but the configured context size is [\d,]+ tokens?",  # DS4
        r"model_context_window_exceeded",  # z.ai finish reason surfaced as an error
        r"prompt too long; exceeded (?:max )?context length",  # Ollama
        r"range of input length should be",  # DashScope / Qwen
        r"context[_ ]length[_ ]exceeded",
        r"too many tokens",
        r"token limit exceeded",
    )
]
_CEREBRAS_OVERFLOW = re.compile(r"^4(?:00|13)\s*(?:status code)?\s*\(no body\)", re.I)
# Throttling text that would otherwise match an overflow pattern ("Too many tokens, please wait").
_NOT_OVERFLOW = [
    re.compile(pattern, re.I)
    for pattern in (
        r"^(Throttling error|Service unavailable):",
        r"rate limit",
        r"too many requests",
    )
]

# Account limits look like throttling but do not clear in seconds.
_NOT_RETRYABLE = re.compile(
    "|".join(
        (
            "GoUsageLimitError",
            "FreeUsageLimitError",
            "Monthly usage limit reached",
            "available balance",
            "insufficient_quota",
            "out of budget",
            "quota exceeded",
            "billing",
            "subscription_sharing_usage_limit_exceeded",
        )
    ),
    re.I,
)
_RETRYABLE = re.compile(
    "|".join(
        (
            "overloaded",
            "currently experiencing high demand",
            "rate.?limit",
            "too many requests",
            "429",
            "500",
            "502",
            "503",
            "504",
            "520",
            "524",
            "service.?unavailable",
            "server.?error",
            "internal.?error",
            "provider.?returned.?error",
            "exceeded request buffer limit while retrying upstream",
            "network.?error",
            "connection.?error",
            "connection.?refused",
            "connection.?lost",
            "other side closed",
            "fetch failed",
            "getaddrinfo",
            "ENOTFOUND",
            "EAI_AGAIN",
            "upstream.?connect",
            "reset before headers",
            "socket hang up",
            "socket connection was closed",
            "timed? out",
            "timeout",
            "terminated",
            "websocket.?closed",
            "websocket.?error",
            "ended without",
            "stream ended before message_stop",
            "stream ended before a terminal response event",
            "http2 request did not get a response",
            "retry delay",
            "you can retry your request",
            "try your request again",
            "please retry your request",
            "ResourceExhausted",
            "subscription_sharing_usage_unavailable",
            "subscription_sharing_user_unavailable",
        )
    ),
    re.I,
)


# Python's own transport failures (httpx, websockets, the standard library), which the
# upstream patterns, written for JavaScript runtimes, do not name.
_PYTHON_TRANSIENT = re.compile(
    "|".join(
        (
            r"\b(?:Connect|Read|Write|Pool)(?:Error|Timeout)\b",
            r"RemoteProtocolError",
            r"ConnectionResetError|ConnectionAbortedError|BrokenPipeError|IncompleteRead",
            r"connection reset",
            r"ConnectionClosed",
            r"server disconnected",
            r"peer closed",
            r"name resolution",
            r"nodename nor servname",
        )
    ),
    re.I,
)
# This library's HTTP failures read "... (HTTP 503; server) ...": decide by the status,
# not by digits that happen to appear in the response body.
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3});")
_RETRYABLE_STATUS = {408, 429}


def _input_tokens(message: AssistantMessage) -> int:
    usage = message.usage or {}
    return int(usage.get("input") or 0) + int(usage.get("cache_read") or 0)


def is_context_overflow(message: AssistantMessage, context_window: int | None = None) -> bool:
    """Whether a response failed because the input exceeded the model's context window.

    Most providers report it as an error message. Pass `context_window` to also catch
    providers that accept an oversized input silently (input usage above the window) or
    truncate it and stop for length with no output.
    """
    error = message.error or ""
    if message.stop_reason == "error" and error:
        if not any(p.search(error) for p in _NOT_OVERFLOW):
            if any(p.search(error) for p in _OVERFLOW):
                return True
            if message.provider == "cerebras" and _CEREBRAS_OVERFLOW.search(error):
                return True
    if context_window and message.stop_reason == "stop":
        if _input_tokens(message) > context_window:
            return True
    if context_window and message.stop_reason == "length":
        if int((message.usage or {}).get("output") or 0) == 0:
            if _input_tokens(message) >= context_window * 0.99:
                return True
    return False


def is_recoverable_length(message: AssistantMessage, desired_max_output: int) -> bool:
    """A length stop below the intended output limit, possibly from context pressure."""
    output = int((message.usage or {}).get("output") or 0)
    return (
        message.stop_reason == "length" and desired_max_output > 0 and output < desired_max_output
    )


def is_retryable_error(message: AssistantMessage) -> bool:
    """Whether a failed response looks transient (overload, rate limit, network, server error).

    Check `is_context_overflow` first: an overflow needs a smaller context, not a retry.
    Quota and billing limits are never retryable.
    """
    error = message.error or ""
    if message.stop_reason != "error" or not error:
        return False
    if _NOT_RETRYABLE.search(error):
        return False
    status = _HTTP_STATUS.search(error)
    if status:
        code = int(status.group(1))
        return code >= 500 or code in _RETRYABLE_STATUS
    return bool(_RETRYABLE.search(error) or _PYTHON_TRANSIENT.search(error))


def retry_delay(attempt: int, base: float = 2.0, max_delay: float = 60.0) -> float:
    """Exponential backoff in seconds for the 1-based retry `attempt`, as in Pi."""
    return min(base * 2 ** max(0, attempt - 1), max_delay)
