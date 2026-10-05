"""Negotiated lossless deltas, scope isolation, and retained-state ownership."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from importlib.metadata import version
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("mcp")
if tuple(int(n) for n in version("mcp").split(".")[:2]) < (2, 3):
    pytest.skip("interactive MCP needs SDK >=2.3", allow_module_level=True)

from mcp import types
from pi_python import (
    AssistantMessage,
    CancelToken,
    ModelEvent,
    ModelRequest,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolDeclaration,
    ToolResultMessage,
    UserMessage,
    UnsupportedCapabilityError,
)
from pi_python.mcp import (
    MCPCallbacks,
    MCPRequestContext,
    SamplingHandler,
    SamplingProfile,
    SamplingProvider,
)
from pi_python._mcp_host import (
    SAMPLING_EXTENSION,
    SAMPLING_DELTA_EXTENSION,
    _SamplingState,
    visible_message,
)
from pi_python._mcp_sampling import to_sampling

CONTEXT = MCPRequestContext("business", "demo", 7, "2025-11-25", "run", "call")
TOOL = ToolDeclaration(
    "lookup", "query", {"type": "object", "properties": {"q": {"type": "string"}}}
)


class Recorder:
    def __init__(self, replies=()):
        self.requests = []
        self.replies = list(replies)

    async def stream(self, request, cancel):
        self.requests.append(deepcopy(request))
        # Deliberate mutation must not alter the delta cache or retained state.
        if request.tools:
            request.tools[0].input_schema["provider_mutation"] = True
        for message in request.messages:
            if isinstance(message, AssistantMessage) and message.tool_calls:
                message.tool_calls[0].arguments["provider_mutation"] = True
        yield ModelEvent.done(self.replies.pop(0) if self.replies else AssistantMessage.text("ok"))


class Session:
    def __init__(self, handler, *, incremental=True):
        self.handler = handler
        self.state = _SamplingState()
        self.context = replace(CONTEXT, _state=self.state)
        self.wire = []
        self.client_capabilities = types.ClientCapabilities(
            sampling=types.SamplingCapability(tools=types.SamplingToolsCapability()),
            experimental={
                SAMPLING_EXTENSION: {},
                **({SAMPLING_DELTA_EXTENSION: {}} if incremental else {}),
            },
        )

    async def send_request(self, request, result_type, **kwargs):
        self.wire.append(request.params.model_dump(by_alias=True, exclude_none=True))
        result = await self.handler(self.context, request.params, CancelToken())
        # Exercise SDK result-type choice even when tools were omitted by a delta.
        return result_type.model_validate(result.model_dump(by_alias=True, exclude_none=True))


def provider(session):
    return SamplingProvider(NS(protocol_version="2025-11-25", request_id=23, session=session))


async def complete(proxy, request, cancel=None):
    events = [event async for event in proxy.stream(request, cancel or CancelToken())]
    return events[-1].message


@pytest.mark.parametrize("incremental", [True, False])
async def test_identical_full_provider_requests_with_new_and_old_peer(incremental):
    host = Recorder()
    session = Session(SamplingHandler(host, model="host"), incremental=incremental)
    request = ModelRequest([SystemMessage("system-" * 200), UserMessage("first")], [TOOL])
    original = deepcopy(request)
    async with provider(session) as proxy:
        response = await complete(proxy, request)
        request.messages += [response, UserMessage("next")]
        await complete(proxy, request)
        # Identical third input => empty suffix is valid, not an empty full history.
        await complete(proxy, request)
        assert proxy._bases or not incremental
    assert not proxy._bases
    assert request.tools == original.tools
    for captured in host.requests:
        assert captured.tools == original.tools
        assert captured.messages[0].content == original.messages[0].content
    assert [len(r.messages) for r in host.requests] == [2, 4, 4]
    if incremental:
        assert "systemPrompt" not in session.wire[1] and "tools" not in session.wire[1]
        assert len(session.wire[1]["messages"]) == 2
        assert session.wire[2]["messages"] == []
        assert session.wire[1]["_meta"][SAMPLING_EXTENSION]["delta"]["prefix"] == 1
    else:
        assert all("systemPrompt" in wire and "tools" in wire for wire in session.wire)
        assert all("delta" not in wire["_meta"][SAMPLING_EXTENSION] for wire in session.wire)
    session.state.clear()
    assert not session.state.requests and not session.state.responses


async def test_tools_signatures_branch_changed_schema_and_removed_static_fields():
    signed = AssistantMessage(
        [
            ThinkingContent("private", "opaque"),
            ToolCall("id", "lookup", {"q": "value"}, thought_signature="secret"),
        ],
        stop_reason="tool_use",
    )
    host = Recorder([signed])
    session = Session(SamplingHandler(host, model="host"))
    req = ModelRequest([SystemMessage("initial"), UserMessage("query")], [deepcopy(TOOL)])
    async with provider(session) as proxy:
        response = await complete(proxy, req)
        assert len(response.content) == 1 and response.tool_calls[0].thought_signature is None
        req.messages.extend([response, ToolResultMessage("id", "lookup", [TextContent("found")])])
        await complete(proxy, req)
        # Fork the visible conversation and change the schema/system, without
        # changing retained assistant content. Host replay must remain exact.
        req.messages[0] = SystemMessage("changed")
        req.messages[1] = UserMessage("branched query")
        req.tools[0].description = "changed schema description"
        await complete(proxy, req)
        assert session.wire[2]["_meta"][SAMPLING_EXTENSION]["delta"]["prefix"] == 0
        assert session.wire[2]["systemPrompt"] == "changed"
        req.messages = [UserMessage("new branch without tools")]
        req.tools = []
        await complete(proxy, req)
    assert host.requests[1].messages[2] == signed
    assert (
        host.requests[2].messages[2] == signed
    )  # Provider mutation did not affect retained state.
    assert host.requests[2].tools[0].description == "changed schema description"
    assert len(host.requests[3].messages) == 1 and not host.requests[3].tools
    assert req.messages[0].content == "new branch without tools"


async def test_changed_retained_assistant_still_rejected_after_delta_expansion():
    host = Recorder()
    session = Session(SamplingHandler(host, model="host"))
    async with provider(session) as proxy:
        req = ModelRequest([UserMessage("start")])
        answer = await complete(proxy, req)
        req.messages += [answer, UserMessage("continue")]
        await complete(proxy, req)
        answer.content[0].text = "tampered"
        with pytest.raises(UnsupportedCapabilityError, match="changed"):
            await complete(proxy, req)
    assert len(host.requests) == 2


@pytest.mark.parametrize(
    "edit",
    [
        {"base": "missing"},
        {"prefix": -1},
        {"prefix": True},
        {"prefix": 999},
        {"reuse": ["temperature"]},
        {"reuse": ["tools", "tools"]},
        {"reuse": [42]},
        {"reuse": "tools"},
        {"extra": True},
    ],
)
async def test_malformed_or_expired_delta_fails_before_provider(edit):
    host = Recorder()
    session = Session(SamplingHandler(host, model="host"))
    async with provider(session) as proxy:
        await complete(proxy, ModelRequest([UserMessage("start")], [TOOL]))
    ref = next(iter(session.state.requests))
    data = deepcopy(session.wire[0])
    data.pop("tools")
    data["messages"] = []
    data["_meta"][SAMPLING_EXTENSION]["delta"] = {
        "base": ref,
        "prefix": 1,
        "reuse": ["tools"],
        **edit,
    }
    with pytest.raises(UnsupportedCapabilityError):
        await session.handler(
            session.context, types.CreateMessageRequestParams.model_validate(data), CancelToken()
        )
    assert len(host.requests) == 1


async def test_delta_scope_profile_and_conflicts():
    host = Recorder()
    session = Session(
        SamplingHandler(host, model="host", profiles={"other": SamplingProfile("host")})
    )
    async with provider(session) as proxy:
        await complete(proxy, ModelRequest([SystemMessage("sys"), UserMessage("start")], [TOOL]))
    ref = next(iter(session.state.requests))
    data = deepcopy(session.wire[0])
    data.pop("tools")
    data.pop("systemPrompt")
    data["messages"] = []
    ext = data["_meta"][SAMPLING_EXTENSION]
    ext["delta"] = {"base": ref, "prefix": 1, "reuse": ["tools", "systemPrompt"]}
    for field, value in [("conversation", "foreign"), ("profile", "other")]:
        bad = deepcopy(data)
        bad["_meta"][SAMPLING_EXTENSION][field] = value
        with pytest.raises(UnsupportedCapabilityError, match="foreign"):
            await session.handler(
                session.context, types.CreateMessageRequestParams.model_validate(bad), CancelToken()
            )
    bad = deepcopy(data)
    bad["systemPrompt"] = "conflicting"
    with pytest.raises(UnsupportedCapabilityError, match="Conflicting"):
        await session.handler(
            session.context, types.CreateMessageRequestParams.model_validate(bad), CancelToken()
        )
    for context in (replace(session.context, _state=_SamplingState()), session.context):
        context._state.clear()
        with pytest.raises(UnsupportedCapabilityError, match="Expired"):
            await session.handler(
                context, types.CreateMessageRequestParams.model_validate(data), CancelToken()
            )
    assert len(host.requests) == 1


async def test_disable_incremental_and_unnegotiated_result():
    handler = SamplingHandler(Recorder(), model="host", incremental=False)
    assert "sampling.delta" not in MCPCallbacks(sampling=handler).capabilities
    assert "sampling-delta-v1" in __import__("pi_python").FEATURES
    data = to_sampling(ModelRequest([UserMessage("start")]), 100)
    data["_meta"] = {
        SAMPLING_EXTENSION: {"profile": "default", "conversation": "c", "history": [], "delta": {}}
    }
    with pytest.raises(UnsupportedCapabilityError, match="unavailable"):
        await handler(
            replace(CONTEXT, _state=_SamplingState()),
            types.CreateMessageRequestParams.model_validate(data),
            CancelToken(),
        )


async def test_concurrent_branches_keep_acknowledged_bases():
    session = Session(SamplingHandler(Recorder(), model="host", max_concurrency=4))
    async with provider(session) as proxy:
        await complete(proxy, ModelRequest([UserMessage("seed")]))
        await asyncio.gather(
            *(complete(proxy, ModelRequest([UserMessage(f"branch {i}")])) for i in range(8))
        )
        await complete(proxy, ModelRequest([UserMessage("last")]))
        assert len(session.state.requests) == 10
    assert not proxy._bases


async def test_cancelled_request_does_not_advance_base_and_exit_clears_it():
    session = Session(SamplingHandler(Recorder(), model="host"))
    original = session.send_request
    started = asyncio.Event()

    async def wait(**kwargs):
        started.set()
        await asyncio.Event().wait()

    async with provider(session) as proxy:
        await complete(proxy, ModelRequest([UserMessage("seed")]))
        base = dict(proxy._bases)
        session.send_request = wait
        cancel = CancelToken()
        task = asyncio.create_task(complete(proxy, ModelRequest([UserMessage("cancel")]), cancel))
        await started.wait()
        cancel.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proxy._bases == base and not proxy._tasks
        session.send_request = original
        await complete(proxy, ModelRequest([UserMessage("seed")]))
        assert session.wire[-1]["messages"] == []
    assert not proxy._bases


def test_projection_never_copies_hidden_state_and_mutable_arguments_are_isolated():
    class CannotCopy:
        def __deepcopy__(self, memo):
            raise AssertionError("hidden state should not be projected")

    original = AssistantMessage(
        [
            ThinkingContent("private", "opaque"),
            ToolCall("id", "lookup", {"items": [1]}, thought_signature="secret"),
        ],
        stop_reason="tool_use",
        diagnostics=[{"private": CannotCopy()}],
    )
    visible = visible_message(original)
    visible.tool_calls[0].arguments["items"].append(2)
    assert original.tool_calls[0].arguments["items"] == [1]
    assert visible.tool_calls[0].thought_signature is None
    assert len(original.content) == 2


async def test_json_boolean_and_number_schema_changes_are_not_identical():
    host = Recorder()
    session = Session(SamplingHandler(host, model="host"))
    req = ModelRequest([UserMessage("query")], [deepcopy(TOOL)])
    req.tools[0].input_schema["properties"]["q"] = {"enum": [1]}
    async with provider(session) as proxy:
        await complete(proxy, req)
        req.tools[0].input_schema["properties"]["q"] = {"enum": [True]}
        await complete(proxy, req)
    assert "tools" in session.wire[1]
    value = host.requests[1].tools[0].input_schema["properties"]["q"]["enum"][0]
    assert type(value) is bool and value is True


async def test_changed_boolean_tool_argument_fails_retained_integrity_check():
    reply = AssistantMessage([ToolCall("id", "lookup", {"q": 1})], stop_reason="tool_use")
    host = Recorder([reply])
    session = Session(SamplingHandler(host, model="host"))
    async with provider(session) as proxy:
        req = ModelRequest([UserMessage("query")], [deepcopy(TOOL)])
        answer = await complete(proxy, req)
        req.messages += [answer, ToolResultMessage("id", "lookup", [TextContent("ok")])]
        await complete(proxy, req)  # primes a base containing the tool arguments
        answer.tool_calls[0].arguments["q"] = True
        with pytest.raises(UnsupportedCapabilityError, match="changed"):
            await complete(proxy, req)
    assert len(host.requests) == 2


async def test_scope_exit_racing_acknowledgment_cannot_repopulate_base(monkeypatch):
    from pi_python import ConfigurationError
    import pi_python._mcp_interaction as interaction

    session = Session(SamplingHandler(Recorder(), model="host"))
    original = interaction._await_cancel
    acknowledged, resume = asyncio.Event(), asyncio.Event()

    async def paused(awaitable, cancel):
        result = await original(awaitable, cancel)
        if isinstance(awaitable, asyncio.Task) and awaitable.get_name() == "mcp-sampling-provider":
            acknowledged.set()
            await resume.wait()
        return result

    monkeypatch.setattr(interaction, "_await_cancel", paused)
    proxy = provider(session)
    async with proxy:
        task = asyncio.create_task(complete(proxy, ModelRequest([UserMessage("query")])))
        await acknowledged.wait()
    assert not proxy._bases
    resume.set()
    with pytest.raises(ConfigurationError, match="scope ended"):
        await task
    assert not proxy._bases and not proxy._tasks


def test_plugin_configuration_accepts_optional_delta_requirement():
    from pi_python.plugins import Plugin, PluginAPI

    api = PluginAPI(Plugin("demo", lambda api: None), None, {}, {})
    api.add_mcp_server(
        "business", {"command": "unused", "required_capabilities": ["sampling.delta"]}
    )
    assert api._mcp["business"]["required_capabilities"] == ["sampling.delta"]
    assert (
        "sampling.delta"
        in MCPCallbacks(sampling=SamplingHandler(Recorder(), model="host")).capabilities
    )


async def test_cancelled_scope_exit_joins_cleanup_and_clears_base():
    session = Session(SamplingHandler(Recorder(), model="host"))
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []

    async def wait(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()
            cleaned.append(True)

    proxy = provider(session)
    await proxy.__aenter__()
    await complete(proxy, ModelRequest([UserMessage("seed")]))
    session.send_request = wait
    caller = asyncio.create_task(complete(proxy, ModelRequest([UserMessage("next")])))
    await started.wait()
    closer = asyncio.create_task(proxy.__aexit__())
    try:
        await cleaning.wait()
        closer.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await closer
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert cleaned == [True]
        assert not proxy._bases and not proxy._tasks
    finally:
        finish.set()
        await asyncio.gather(closer, caller, return_exceptions=True)


@pytest.mark.parametrize("disconnect", [False, True])
async def test_cancelled_host_cleanup_clears_retained_state(disconnect):
    from pi_python import ToolContext
    from pi_python._mcp_interaction import _Interaction

    async def emit(event):
        pass

    context = ToolContext("run", "call", CancelToken(), emit)
    interaction = _Interaction(MCPCallbacks(), "business", None)
    call = interaction.call(context)
    await call.__aenter__()
    scope = interaction.scope
    scope.state.save("conversation", "default", AssistantMessage.text("private"))
    scope.state.save_request("base", "conversation", "default", {"messages": []})
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()
            cleaned.append(True)

    child = asyncio.create_task(work())
    scope.tasks.add(child)
    await started.wait()
    closer = asyncio.create_task(
        interaction.aclose() if disconnect else call.__aexit__(None, None, None)
    )
    try:
        await cleaning.wait()
        closer.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert cleaned == [True]
        assert not scope.state.responses and not scope.state.requests
    finally:
        finish.set()
        await asyncio.gather(closer, child, return_exceptions=True)
        if disconnect:
            await call.__aexit__(None, None, None)
