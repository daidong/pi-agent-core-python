"""Codex cache affinity is Agent-owned and independent of transport sessions."""

import asyncio
import base64
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import httpx
import pytest

from pi_python import *
from pi_python.providers import HTTPTransport, OpenAICodexProvider, OpenAIProvider


FIXTURES = Path(__file__).resolve().parents[1] / "compat/provider-fixtures"
TOKEN = (
    "x."
    + base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "fixture"}}).encode()
    )
    .decode()
    .rstrip("=")
    + ".y"
)


def reply(name="openai-text"):
    events = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["events"]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(f"data: {json.dumps(event)}\n\n" for event in events),
    )


@pytest.fixture
async def wire():
    requests, responses = [], []

    async def handle(request):
        requests.append(request)
        return responses.pop(0) if responses else reply()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OpenAICodexProvider(api_key=TOKEN, transport=HTTPTransport(client))
        yield provider, requests, responses
        await provider.aclose()


def cache_identity(request):
    key = json.loads(request.content).get("prompt_cache_key")
    assert request.headers.get("session-id") == key
    assert request.headers.get("x-client-request-id") == key
    return key


async def test_default_codex_agent_sends_stable_session_headers(wire):
    provider, requests, _ = wire
    agent = Agent(provider=provider, model="gpt-6.1-sol")
    assert (await agent.prompt("first")).status == "completed"
    assert (await agent.prompt("second")).status == "completed"
    identity = cache_identity(requests[0])
    assert isinstance(identity, str) and 0 < len(identity) <= 64
    assert cache_identity(requests[1]) == identity


async def test_codex_cache_id_survives_tool_loop_config_replacement_and_retry(wire):
    provider, requests, responses = wire
    responses.extend([reply("openai-tool"), reply()])

    @tool
    def echo(value: str) -> str:
        return value

    agent = Agent(provider=provider, model="gpt-6.1-sol", tools=[echo])
    identity = agent.cache_session_id
    result = await agent.prompt("use echo")
    assert result.status == "completed", result.errors
    assert len(requests) == 2 and result.tool_outcomes[0].result.content[0].text == "hello"
    agent.update_config(AgentConfigUpdate(options={"temperature": 0.2}))
    responses.append(httpx.Response(400, json={"error": {"message": "fixture failure"}}))
    assert (await agent.prompt("retry later")).status == "failed"
    assert (await agent.continue_run()).status == "completed"
    assert identity and {cache_identity(request) for request in requests} == {identity}
    assert len(requests) == 4


async def test_reset_and_restore_preserve_only_the_intended_cache_session(wire):
    provider, requests, _ = wire
    agent = Agent(provider=provider, model="gpt-6.1-sol")
    assert (await agent.prompt("remember this")).status == "completed"
    identity = agent.cache_session_id
    history = encode_messages(list(agent.state.messages))
    restored = Agent(
        provider=provider,
        model="gpt-6.1-sol",
        messages=decode_messages(history),
        cache_session_id=identity,
    )
    assert (await restored.prompt("continue")).status == "completed"
    assert cache_identity(requests[-1]) == identity
    assert len(json.loads(requests[-1].content)["input"]) >= 3
    restored.reset()
    assert restored.cache_session_id != identity
    assert (await restored.prompt("new conversation")).status == "completed"
    assert cache_identity(requests[-1]) == restored.cache_session_id
    assert agent.cache_session_id == identity


async def test_agents_sharing_a_provider_have_independent_cache_ids(wire):
    provider, requests, _ = wire
    agents = [Agent(provider=provider, model="gpt-6.1-sol") for _ in range(3)]
    results = await asyncio.gather(*(agent.prompt("hello") for agent in agents))
    assert all(result.status == "completed" for result in results)
    assert len({cache_identity(request) for request in requests}) == 3


async def test_nested_agent_keeps_its_own_cache_session(wire):
    provider, requests, responses = wire
    responses.extend([reply("openai-tool"), reply(), reply()])
    child = Agent(provider=provider, model="gpt-6.1-sol")

    @tool
    async def echo(value: str, context: ToolContext) -> str:
        result = await context.run_agent(child, value)
        assert result.status == "completed"
        return "child completed"

    parent = Agent(provider=provider, model="gpt-6.1-sol", tools=[echo])
    assert (await parent.prompt("delegate")).status == "completed"
    assert [cache_identity(request) for request in requests] == [
        parent.cache_session_id,
        child.cache_session_id,
        parent.cache_session_id,
    ]
    assert child.cache_session_id != parent.cache_session_id


@pytest.mark.parametrize("where", ["constructor", "options", "update", "hook"])
async def test_explicit_session_id_has_precedence_and_survives_reset(wire, where):
    provider, requests, _ = wire
    options = {"session_id": "from-options"}
    kwargs = {"session_id": "explicit"} if where == "constructor" else {}
    if where == "options":
        options["session_id"] = "explicit"
    if where == "hook":
        kwargs["hooks"] = Hooks(
            prepare_request=lambda ctx, cancel: TurnUpdate(options={"session_id": "explicit"})
        )
    agent = Agent(provider=provider, model="gpt-6.1-sol", options=options, **kwargs)
    if where == "update":
        agent.update_config(AgentConfigUpdate(options={"session_id": "explicit"}))
    for _ in range(2):
        assert (await agent.prompt("hello")).status == "completed"
        assert cache_identity(requests[-1]) == "explicit"
        agent.reset()


@pytest.mark.parametrize("disabled", ["automatic", "retention"])
async def test_opt_out_omits_auto_cache_fields(wire, disabled):
    provider, requests, _ = wire
    agent = Agent(
        provider=provider,
        model="gpt-6.1-sol",
        auto_session_id=disabled != "automatic",
        options={"cache_retention": "none"} if disabled == "retention" else {},
    )
    assert (await agent.prompt("hello")).status == "completed"
    assert cache_identity(requests[-1]) is None
    if disabled == "automatic":
        assert agent.cache_session_id is None


async def test_opt_out_does_not_disable_explicit_session_and_retention_none_does(wire):
    provider, requests, _ = wire
    agent = Agent(
        provider=provider, model="gpt-6.1-sol", auto_session_id=False, session_id="explicit"
    )
    assert (await agent.prompt("hello")).status == "completed"
    assert cache_identity(requests[-1]) == "explicit"
    agent.update_config(
        AgentConfigUpdate(options={"session_id": "explicit", "cache_retention": "none"})
    )
    assert (await agent.prompt("hello")).status == "completed"
    assert cache_identity(requests[-1]) is None


async def test_low_level_provider_does_not_invent_a_session(wire):
    provider, requests, _ = wire
    request = ModelRequest([UserMessage("hello")], model="gpt-6.1-sol")
    async for _ in provider.stream(request, CancelToken()):
        pass
    assert cache_identity(requests[-1]) is None


async def test_automatic_headers_preserve_case_insensitive_application_overrides(wire):
    provider, requests, _ = wire
    agent = Agent(
        provider=provider,
        model="gpt-6.1-sol",
        options={"headers": {"Session-Id": "app-routing", "X-Client-Request-Id": "app-request"}},
    )
    assert (await agent.prompt("hello")).status == "completed"
    assert requests[-1].headers["session-id"] == "app-routing"
    assert requests[-1].headers["x-client-request-id"] == "app-request"


async def test_default_provider_routes_cache_id_only_to_codex(wire):
    provider, requests, _ = wire
    agent = Agent(model="gpt-6.1-sol")
    try:
        set_default_stream_fn(provider)
        assert (await agent.prompt("hello")).status == "completed"
        assert cache_identity(requests[-1]) == agent.cache_session_id
        identity = agent.cache_session_id
        set_default_stream_fn(ScriptedProvider([AssistantMessage.text("custom")]))
        assert agent.cache_session_id is None
        assert (await agent.prompt("custom")).status == "completed"
        set_default_stream_fn(provider)
        assert agent.cache_session_id == identity
    finally:
        set_default_stream_fn(None)


async def test_public_request_options_and_dataclass_schema_stay_unchanged(wire):
    provider, _, _ = wire
    seen = []
    original = provider.build_request

    def build(request, chatgpt_sign_in=False):
        # A request-copying adapter retains metadata, but old dataclass consumers
        # still see exactly the old fields and caller-supplied options.
        seen.append(deepcopy(request))
        return original(deepcopy(request), chatgpt_sign_in)

    provider.build_request = build
    agent = Agent(provider=provider, model="gpt-6.1-sol", options={"temperature": 0.2})
    assert (await agent.prompt("hello")).status == "completed"
    request = seen[0]
    assert request.options == {"temperature": 0.2}
    assert set(asdict(request)) == {
        "messages",
        "tools",
        "model",
        "options",
        "model_info",
        "api_key",
        "on_payload",
        "on_response",
        "on_provider_stream_event",
    }
    assert original(request)["prompt_cache_key"] == agent.cache_session_id


async def test_other_providers_and_function_streams_do_not_receive_auto_metadata(wire):
    codex, requests, _ = wire
    provider = OpenAIProvider(api_key="sk-fixture", transport=codex.transport)
    agent = Agent(provider=provider, model="gpt-6.1-sol")
    assert agent.cache_session_id is None
    assert (await agent.prompt("hello")).status == "completed"
    assert cache_identity(requests[-1]) is None
    recorded = []

    async def stream(request, cancel):
        recorded.append(request)
        yield ModelEvent.done(AssistantMessage.text("custom"))

    custom = Agent(stream_fn=stream)
    assert (await custom.prompt("hello")).status == "completed"
    assert not hasattr(recorded[0], "_agent_cache_session_id")
    assert recorded[0].options == {}


async def test_cache_capability_is_optional_and_independent_of_names_and_models():
    class CustomProvider(ScriptedProvider):
        supports_agent_cache_session = True

    provider = CustomProvider([AssistantMessage.text("custom")])
    agent = Agent(provider=provider, model="anything")
    assert (await agent.prompt("hello")).status == "completed"
    assert provider.requests[0].cache_session_id == agent.cache_session_id
    assert agent.cache_session_id
    assert provider.requests[0].options == {}

    # A name or a model record is not a capability declaration.
    plain = ScriptedProvider([AssistantMessage.text("plain")])
    plain.name = "openai-codex"
    agent.provider = plain
    assert (await agent.prompt("different channel")).status == "completed"
    assert agent.cache_session_id is None
    assert plain.requests[0].cache_session_id is None
    agent.provider = provider
    assert agent.cache_session_id == provider.requests[0].cache_session_id


@pytest.mark.parametrize("value", [None, 1, "yes"])
def test_auto_session_flag_is_boolean(value):
    with pytest.raises(ConfigurationError, match="auto_session_id"):
        Agent(auto_session_id=value)


@pytest.mark.parametrize("value", ["", "x" * 65, "with space", "line\nbreak", "é", 3])
def test_restored_cache_id_is_validated(value):
    with pytest.raises(ConfigurationError, match="cache_session_id"):
        Agent(cache_session_id=value)


def test_restored_cache_session_requires_enabled_automatic_handling():
    with pytest.raises(ConfigurationError, match="auto_session_id"):
        Agent(auto_session_id=False, cache_session_id="restored")
