import pytest
from pi_python import *


def failed(error, provider="anthropic", stop_reason="error", **usage):
    return AssistantMessage(
        [TextContent("")], stop_reason, provider=provider, error=error, usage=usage
    )


@pytest.mark.parametrize(
    "error",
    [
        "prompt is too long: 213462 tokens > 200000 maximum",
        'ProviderHTTPError: Provider request failed (HTTP 400; request): {"type":"error","error":'
        '{"type":"invalid_request_error","message":"prompt is too long: 210000 tokens > 200000"}}',
        "Your input exceeds the context window of this model",
        "Input length (265330) exceeds model's maximum context length (262144).",
        "the request exceeds the available context size, try increasing it",
        "prompt too long; exceeded max context length by 1200 tokens",
    ],
)
def test_overflow_messages_from_several_providers(error):
    message = failed(error)
    assert is_context_overflow(message) and not is_retryable_error(message)


def test_overflow_edge_cases():
    assert not is_context_overflow(
        failed("Throttling error: Too many tokens, please wait before trying again.")
    )
    assert is_context_overflow(failed("413 status code (no body)", provider="cerebras"))
    assert not is_context_overflow(failed("413 status code (no body)", provider="openai"))
    silent = AssistantMessage.text("ok", usage={"input": 900, "cache_read": 200})
    assert is_context_overflow(silent, context_window=1000) and not is_context_overflow(silent)
    truncated = AssistantMessage.text("", stop_reason="length", usage={"input": 995, "output": 0})
    assert is_context_overflow(truncated, context_window=1000)
    assert is_recoverable_length(truncated, desired_max_output=4096)


@pytest.mark.parametrize(
    "error, retryable",
    [
        ("ProviderHTTPError: Provider request failed (HTTP 529; server): overloaded_error", True),
        ("ProviderHTTPError: Provider request failed (HTTP 429; rate_limit)", True),
        ("ProviderProtocolError: Stream ended without final message", True),
        ("ConnectError: connection refused", True),
        (
            "ProviderHTTPError: Provider request failed (HTTP 429; rate_limit): insufficient_quota",
            False,
        ),
        ("ProviderHTTPError: Provider request failed (HTTP 401; authentication)", False),
    ],
)
def test_retryable_errors(error, retryable):
    assert is_retryable_error(failed(error)) is retryable
    assert not is_retryable_error(failed(error, stop_reason="aborted"))


def test_retry_delay_backs_off_and_caps():
    assert [retry_delay(n) for n in (1, 2, 3)] == [2.0, 4.0, 8.0]
    assert retry_delay(10) == 60.0


async def test_continue_run_retries_a_failed_response():
    class Flaky:
        def __init__(self):
            self.requests = []

        async def stream(self, request, cancel):
            self.requests.append([m.role for m in request.messages])
            if len(self.requests) == 1:
                raise RuntimeError("503 service unavailable")
            yield ModelEvent.done(AssistantMessage.text("recovered"))

    provider = Flaky()
    agent = Agent(provider=provider)
    first = await agent.prompt("go")
    assert first.status == "failed" and is_retryable_error(first.messages[-1])
    second = await agent.continue_run()
    assert second.status == "completed" and second.messages[-1].content[0].text == "recovered"
    assert [m.role for m in agent.state.messages] == ["user", "assistant", "assistant"]
    with pytest.raises(InvalidContinuationError):
        await agent.continue_run()  # a successful answer still needs new input
