"""Approved default timing migration and request identity at application boundaries."""

import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest
import httpx
from pi_python import *
import pi_python.stream as stream_module
import pi_python.messages as messages_module
import pi_python.tools as tools_module
from pi_python.stream import checked_events, event_contract
from tests.test_providers import events, remote
from pi_python.providers import HTTPTransport, OpenAICompletionsProvider
from tests.test_completions import chunk, sse


@pytest.mark.parametrize("failure", [False, True])
async def test_tool_duration_excludes_before_and_after_hooks(monkeypatch, failure):
    clock = [0.0]
    monkeypatch.setattr(tools_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def before(call, args, context):
        clock[0] += 10

    def after(call, result, context):
        clock[0] += 20

    async def execute(args, context):
        clock[0] += 0.125
        if failure:
            raise ValueError("tool failed")
        return ToolResult.text("ok")

    provider = ScriptedProvider(
        [
            AssistantMessage([ToolCall("c", "t", {})], "tool_use"),
            AssistantMessage.text("done"),
        ]
    )
    agent = Agent(
        provider=provider,
        tools=[Tool("t", "", {}, execute)],
        hooks=Hooks(before_tool_call=before, after_tool_call=after),
    )
    seen = []
    agent.subscribe(seen.append)
    result = await agent.prompt("go")
    assert result.status == "completed"
    outcome = result.tool_outcomes[0]
    assert outcome.duration_ms == 125
    assert outcome.execution_status == ("failed" if failure else "succeeded")
    message = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    assert message.duration_ms == 125
    assert next(e for e in seen if e.type == "tool_execution_end").data["duration_ms"] == 125
    assert decode_messages(encode_messages(agent.state.messages)) == list(agent.state.messages)


async def test_not_started_tools_omit_timing_but_dataclass_has_optional_field():
    agent = Agent(
        provider=ScriptedProvider(
            [
                AssistantMessage([ToolCall("c", "missing", {})], "tool_use"),
                AssistantMessage.text("done"),
            ]
        )
    )
    seen = []
    agent.subscribe(seen.append)
    result = await agent.prompt("go")
    assert result.status == "completed"
    outcome = result.tool_outcomes[0]
    assert outcome.execution_status == "not_started" and outcome.duration_ms is None
    assert asdict(outcome)["duration_ms"] is None
    message = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    assert "duration_ms" not in message_to_dict(message)
    assert "duration_ms" not in next(e for e in seen if e.type == "tool_execution_end").data


async def test_cancelled_tool_records_duration_after_execution_stops(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(tools_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    entered = asyncio.Event()

    async def execute(args, context):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            clock[0] += 0.25

    agent = Agent(tools=[Tool("t", "", {}, execute)])
    task = asyncio.create_task(agent.execute_tools([ToolInvocation("t", {})]))
    await entered.wait()
    agent.abort()
    result = await task
    assert result.status == "cancelled"
    assert result.tool_outcomes[0].duration_ms == 250
    assert result.messages[0].data["calls"][0]["duration_ms"] == 250


@pytest.mark.parametrize("failure", ["error", "abort"])
async def test_direct_stream_errors_are_timed(monkeypatch, failure):
    clock = [10.0]
    monkeypatch.setattr(
        stream_module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 0.0)
    )

    class Provider:
        name = "fixture"

        @event_contract
        async def stream(self, request, cancel):
            yield ModelEvent("start")
            clock[0] += 0.25
            if failure == "abort":
                raise asyncio.CancelledError
            raise ValueError("failure")

    seen = [e async for e in Provider().stream(ModelRequest([]), CancelToken())]
    assert seen[-1].type == "error" and seen[-1].message.duration_ms == 250
    assert seen[-1].reason == ("aborted" if failure == "abort" else "error")


async def test_custom_provider_exception_is_timed_at_agent_boundary():
    async def stream(request, cancel):
        yield ModelEvent("start")
        raise ValueError("custom failure")

    result = await Agent(stream_fn=stream).prompt("go")
    assert result.status == "failed"
    assert isinstance(result.messages[-1].duration_ms, int)


@pytest.mark.parametrize("reason", ["stop", "deferred", "error", "aborted"])
@pytest.mark.parametrize("timestamp", [99.0, 100.0, 101.0])
@pytest.mark.parametrize("existing", [None, 0, 42])
async def test_timing_uses_message_age_and_preserves_existing_duration(
    monkeypatch, reason, timestamp, existing
):
    clock = [0.0]
    monkeypatch.setattr(
        stream_module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 100.0)
    )
    message = AssistantMessage([], stop_reason=reason, timestamp=timestamp, duration_ms=existing)

    async def stream():
        clock[0] += 0.125
        yield (
            ModelEvent("error", message=message, reason=reason)
            if reason in {"error", "aborted"}
            else ModelEvent.done(message)
        )

    seen = [e async for e in checked_events(stream(), AssistantMessage([]))]
    expected = existing if existing is not None or timestamp < 100.0 else 125
    assert seen[-1].message.duration_ms == expected
    assert message.duration_ms == existing


@pytest.mark.parametrize("kind", ["anthropic", "openai", "completions"])
async def test_remote_timestamp_is_request_start_not_completion(monkeypatch, kind):
    # Move the wall clock inside the request callback, before the response arrives.
    clock = [100.0]
    fake_time = SimpleNamespace(time=lambda: clock[0])
    monkeypatch.setattr(messages_module, "time", fake_time)
    monkeypatch.setattr(
        stream_module, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    )
    model_info = None
    if kind == "completions":
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=sse([chunk({"content": "ok"}), chunk(finish="stop")])
                )
            )
        )
        provider = OpenAICompletionsProvider(
            base_url="https://fixture.test", api_key="fixture", transport=HTTPTransport(client)
        )
        model_info = provider.model("test-model")
    else:
        provider, _, _, client = remote(kind, [events(kind + "-text")])

    def payload(body):
        clock[0] = 103.0

    request = ModelRequest(
        [UserMessage("go")], model="test-model", model_info=model_info, on_payload=payload
    )
    async with client:
        seen = [e async for e in provider.stream(request, CancelToken())]
    assert seen[-1].type == "done"
    assert seen[-1].message.timestamp == 100.0
    assert seen[-1].message.duration_ms == 3000


@pytest.mark.parametrize("existing", [None, 0, 42])
async def test_exception_does_not_time_old_snapshot_or_replace_its_duration(existing):
    class Provider:
        name = "fixture"

        @event_contract
        async def stream(self, request, cancel):
            yield ModelEvent(
                "start", partial=AssistantMessage([], timestamp=1, duration_ms=existing)
            )
            raise ValueError("failed after forwarding old snapshot")

    seen = [e async for e in Provider().stream(ModelRequest([]), CancelToken())]
    assert seen[-1].type == "error"
    assert seen[-1].message.duration_ms == existing


def test_schema_migration_reads_old_history_and_events_and_preserves_new_timing():
    require_features(["default-duration-ms-v1", "client-identity-v1"])
    old = AssistantMessage.text("old")
    history = json.loads(encode_messages([old]))
    assert history["schema_version"] == 4
    history["schema_version"] = 3
    assert decode_messages(json.dumps(history)) == [old]
    old.duration_ms = 0
    assert decode_messages(encode_messages([old])) == [old]
    event = Event("tool_execution_end", "r", 1, 1, data={"duration_ms": 125})
    assert json.loads(encode_event(event))["schema_version"] == 2
    legacy = asdict(event)
    legacy["schema_version"] = 1
    legacy["data"] = {}
    assert decode_event(json.dumps(legacy)).schema_version == 1


@pytest.mark.parametrize("bad", [-1, True, 1.5, "1"])
def test_duration_rejects_invalid_history(bad):
    with pytest.raises(MessageValidationError, match="duration_ms"):
        message_to_dict(AssistantMessage.text("bad", duration_ms=bad))


def test_identity_attribution_is_explicit_and_scoped():
    assert set(get_identity_headers("openrouter", "https://openrouter.ai")) == {"user-agent"}
    identity = {
        "user_agent": "short",
        "attribution": {
            "name": "myapp",
            "url": "https://example.test",
            "categories": ["cli-agent", "test"],
        },
    }
    headers = get_identity_headers("custom", "https://openrouter.ai/api/v1", identity)
    assert headers == {
        "user-agent": "pi-python",
        "http-referer": "https://example.test",
        "x-openrouter-title": "myapp",
        "x-openrouter-categories": "cli-agent,test",
    }
    assert (
        get_identity_headers("nvidia", "https://fixture.test", identity)["x-billing-invoke-origin"]
        == "Myapp"
    )
    assert (
        get_identity_headers("opencode-go", "https://fixture.test", identity)["x-opencode-client"]
        == "myapp"
    )
    assert set(get_identity_headers("custom", "https://openrouter.ai.example.test", identity)) == {
        "user-agent"
    }
    assert set(
        get_identity_headers(
            "openrouter", "https://fixture.test", {**identity, "attribution": None}
        )
    ) == {"user-agent"}
    assert format_user_agent({"user_agent": {"custom": "app/1"}}) == "app/1"
    assert format_user_agent({"version": "test"}).startswith("pi-python/test (")


@pytest.mark.parametrize("kind", ["anthropic", "openai"])
@pytest.mark.parametrize("override", ["identity", "explicit", "remove"])
async def test_identity_reaches_real_request_and_explicit_headers_win(kind, override):
    provider, requests, _, client = remote(kind, [events(kind + "-text")])
    options = {"identity": {"user_agent": {"custom": "app/1"}}}
    if override != "identity":
        options["headers"] = {"User-Agent": "explicit/2" if override == "explicit" else None}
    async with client:
        result = await Agent(provider=provider, model="test-model", options=options).prompt("go")
    assert result.status == "completed", result.errors
    assert (
        requests[0].headers.get("user-agent")
        == {"identity": "app/1", "explicit": "explicit/2", "remove": None}[override]
    )


async def test_agent_identity_survives_option_replacement_and_is_copied():
    identity = {
        "user_agent": "short",
        "attribution": {"name": "app", "url": "https://example.test"},
    }
    provider = ScriptedProvider([AssistantMessage.text("one"), AssistantMessage.text("two")])
    agent = Agent(provider=provider, identity=identity, options={"identity": {"attribution": None}})
    identity["user_agent"] = "detailed"
    copy = agent.identity
    copy["attribution"]["name"] = "mutated"
    await agent.prompt("one")
    assert provider.requests[0].options["identity"] == {"user_agent": "short", "attribution": None}
    agent.update_config(AgentConfigUpdate(options={"temperature": 0.1}))
    await agent.prompt("two")
    assert provider.requests[1].options["identity"] == {**identity, "user_agent": "short"}
    assert agent.identity["attribution"]["name"] == "app"


async def test_provider_cannot_mutate_next_turn_identity():
    seen = []

    async def execute(args, context):
        return ToolResult.text("ok")

    async def stream(request, cancel):
        seen.append(deepcopy(request.options["identity"]))
        request.options["identity"]["attribution"]["name"] = "mutated"
        yield ModelEvent.done(
            AssistantMessage([ToolCall("c", "t", {})], "tool_use")
            if len(seen) == 1
            else AssistantMessage.text("done")
        )

    identity = {"attribution": {"name": "app", "url": "https://example.test"}}
    agent = Agent(stream_fn=stream, tools=[Tool("t", "", {}, execute)], identity=identity)
    assert (await agent.prompt("go")).status == "completed"
    assert seen == [identity, identity]


async def test_anthropic_oauth_keeps_required_user_agent():
    provider, requests, _, client = remote("anthropic", [events("anthropic-text")])
    provider.api_key = "sk-ant-oat-fixture"
    async with client:
        result = await Agent(
            provider=provider, model="test-model", identity={"user_agent": "short"}
        ).prompt("go")
    assert result.status == "completed"
    assert requests[0].headers["user-agent"] == "claude-cli/2.1.280"


async def test_completions_identity_attribution_and_explicit_removal_reach_transport():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=sse([chunk({"content": "ok"}), chunk(finish="stop")]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompletionsProvider(
            name="openrouter",
            base_url="https://fixture.test",
            api_key="fixture",
            transport=HTTPTransport(client),
        )
        agent = Agent(
            provider=provider,
            model=provider.model("fixture"),
            identity={
                "user_agent": {"custom": "app/1"},
                "attribution": {"name": "app", "url": "https://example.test"},
            },
            options={"headers": {"HTTP-Referer": None}},
        )
        result = await agent.prompt("go")
    assert result.status == "completed", result.errors
    assert seen[0].headers["user-agent"] == "app/1"
    assert seen[0].headers["x-openrouter-title"] == "app"
    assert "http-referer" not in seen[0].headers


@pytest.mark.parametrize(
    "identity", [{"userAgent": "short"}, {"user_agent": "bad"}, {"attribution": {}}, {"version": 1}]
)
def test_invalid_identity_is_rejected_before_run(identity):
    with pytest.raises(ConfigurationError):
        Agent(identity=deepcopy(identity))
