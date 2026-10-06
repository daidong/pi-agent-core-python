import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from importlib.metadata import version
import json
import os
import signal
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("mcp")
if tuple(int(n) for n in version("mcp").split(".")[:2]) < (2, 3):
    pytest.skip("interactive MCP needs SDK >=2.3", allow_module_level=True)

from mcp import types
from pi_python import *
from pi_python.mcp import (
    ElicitationHandler,
    ElicitationRequest,
    ElicitationResponse,
    MCPCallbacks,
    MCPRequestContext,
    SamplingHandler,
    SamplingFailure,
    SamplingProvider,
    connect_http,
    connect_stdio,
)
from pi_python._mcp_sampling import (
    from_sampling,
    from_sampling_result,
    to_sampling,
    to_sampling_result,
)
from pi_python.plugins import Plugin, load_plugins

SERVER = Path(__file__).parent / "servers" / "mcp_interactive_server.py"
CONTEXT = MCPRequestContext("business", "demo", 7, "2025-11-25", "run", "call")


async def emit(value):
    pass


def tool_context(label="outer"):
    return ToolContext(label, label, CancelToken(), emit)


class EchoProvider:
    def __init__(self):
        self.requests = []
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def stream(self, request, cancel):
        self.requests.append(deepcopy(request))
        results = [m for m in request.messages if isinstance(m, ToolResultMessage)]
        label = next(m.content[0].text for m in request.messages if isinstance(m, UserMessage))
        if request.tools and not results:
            yield ModelEvent.done(
                AssistantMessage(
                    [
                        ToolCall(f"{label}-a", "decorate", {"text": label}),
                        ToolCall(f"{label}-b", "decorate", {"text": label + "-second"}),
                    ],
                    "tool_use",
                )
            )
        else:
            yield ModelEvent.done(
                AssistantMessage.text(
                    " ".join(m.content[0].text for m in results) if results else label
                )
            )


async def choose(request, cancel):
    return ElicitationResponse(
        "accept", {"style": "short", "confirmed": True, "note": request.context.run_id}
    )


@pytest.fixture(params=["stdio", "http"])
async def endpoint(request):
    if request.param == "stdio":
        if sys.implementation.name != "cpython":
            pytest.skip("SDK stdio server needs CPython")
        yield {"command": sys.executable, "args": [str(SERVER)]}
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, str(SERVER), "--http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("HTTP MCP test process failed to start")
                await asyncio.sleep(0.02)
        yield {"url": f"http://127.0.0.1:{port}/mcp"}
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


@asynccontextmanager
async def connected(endpoint, callbacks=None):
    connect = connect_http if "url" in endpoint else connect_stdio
    async with connect(**endpoint, callbacks=callbacks, server_name="business") as tools:
        yield {t.name: t for t in tools}


async def test_real_nested_agent_and_forms_concurrent_tasks(endpoint):
    provider = EchoProvider()
    observed = []

    async def human(request, cancel):
        observed.append(request)
        await asyncio.sleep(0.01)
        return await choose(request, cancel)

    callbacks = MCPCallbacks(
        sampling=SamplingHandler(provider, model="host-only"), elicitation=ElicitationHandler(human)
    )
    async with connected(endpoint, callbacks) as tools:
        caps = json.loads((await tools["capabilities"].execute({}, tool_context())).content[0].text)
        assert caps["protocol"] == "2025-11-25"
        assert caps["capabilities"]["sampling"] == {"tools": {}}
        assert caps["capabilities"]["elicitation"] == {"form": {}}
        outputs = await asyncio.wait_for(
            asyncio.gather(
                *[
                    tools["workflow"].execute({"label": label}, tool_context(label))
                    for label in ("one", "two")
                ]
            ),
            10,
        )
        for label, result in zip(("one", "two"), outputs):
            assert not result.is_error, result
            data = json.loads(result.content[0].text)
            assert data["model_reply"] == f"[{label}] [{label}-second]"
            assert data["selection"]["note"] == label
        assert [r.context.run_id for r in observed] == ["one", "two"]
    assert not provider.closed
    assert len(provider.requests) == 4
    for request in provider.requests:
        assert request.model == "host-only" and request.api_key is None


@pytest.mark.parametrize("stage", ["sampling", "elicitation"])
@pytest.mark.parametrize("cancellation", ["token", "task"])
async def test_cancel_outer_wait_and_reuse_session(endpoint, stage, tmp_path, cancellation):
    started, stopped = asyncio.Event(), asyncio.Event()
    blocking = True

    class WaitProvider(EchoProvider):
        async def stream(self, request, cancel):
            if blocking:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            async for event in super().stream(request, cancel):
                yield event

    async def human(request, cancel):
        if blocking:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return await choose(request, cancel)

    callbacks = MCPCallbacks(
        sampling=SamplingHandler(WaitProvider(), model="host"),
        elicitation=ElicitationHandler(human),
    )
    async with connected(endpoint, callbacks) as tools:
        marker = tmp_path / "server-finally"
        name = "sample" if stage == "sampling" else "ask"
        context = tool_context()
        task = asyncio.create_task(
            tools[name].execute({"label": "blocked", "marker": str(marker)}, context)
        )
        await asyncio.wait_for(started.wait(), 5)
        if cancellation == "token":
            context.cancel.cancel("test abort")
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(stopped.wait(), 5)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert marker.exists(), "server request's finally did not run"
        blocking = False
        result = await asyncio.wait_for(
            tools[name].execute({"label": "next"}, tool_context("next")), 5
        )
        assert not result.is_error, result


async def test_unconfigured_capabilities_and_old_tools(endpoint):
    async with connected(endpoint) as tools:
        result = await tools["capabilities"].execute({}, tool_context())
        caps = json.loads(result.content[0].text)["capabilities"]
        assert "sampling" not in caps and "elicitation" not in caps
        with pytest.raises(Exception, match="Elicitation not supported"):
            await tools["ask"].execute({"label": "denied"}, tool_context())


def sampling_params(**kwargs):
    return types.CreateMessageRequestParams.model_validate(
        {
            "messages": [{"role": "user", "content": {"type": "text", "text": "hello"}}],
            "maxTokens": 20,
            **kwargs,
        }
    )


def form_params(**kwargs):
    return types.ElicitRequestFormParams.model_validate(
        {
            "message": "Choose",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 2},
                    "choice": {"type": "string", "enum": ["a", "b"]},
                    "confirm": {"type": "boolean"},
                },
                "required": ["text", "choice", "confirm"],
            },
            **kwargs,
        }
    )


def test_sampling_roundtrip_preserves_multiple_tool_ids_and_names():
    request = ModelRequest(
        [
            SystemMessage("system", sections={"policy": "policy text"}),
            UserMessage("question"),
            AssistantMessage(
                [
                    TextContent("checking"),
                    ToolCall("b", "two", {"x": 2}),
                    ToolCall("a", "one", {"x": 1}),
                ],
                "tool_use",
            ),
            ToolResultMessage("a", "one", [TextContent("1")]),
            ToolResultMessage("b", "two", [TextContent("2")], is_error=True),
        ],
        tools=[
            ToolDeclaration(n, n, {"type": "object", "properties": {"x": {"type": "integer"}}})
            for n in ("one", "two")
        ],
        options={"max_tokens": 30, "tool_choice": "auto"},
    )
    data = to_sampling(request, 100)
    assert len(data["messages"][-1]["content"]) == 2
    copy = from_sampling(data)
    assert copy.tools == request.tools
    assert copy.messages[0].content == "system\n\npolicy text"
    assert copy.messages[2].tool_calls == request.messages[2].tool_calls
    assert [(m.call_id, m.name, m.content, m.is_error) for m in copy.messages[3:]] == [
        (m.call_id, m.name, m.content, m.is_error) for m in request.messages[3:]
    ]


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown", "mixed", "name"])
def test_invalid_tool_pairs_are_rejected(change):
    request = ModelRequest(
        [
            UserMessage("q"),
            AssistantMessage([ToolCall("a", "one", {}), ToolCall("b", "two", {})], "tool_use"),
            ToolResultMessage("a", "one", [TextContent("a")]),
            ToolResultMessage("b", "two", [TextContent("b")]),
        ]
    )
    if change == "missing":
        request.messages.pop()
    elif change == "duplicate":
        request.messages[-1] = request.messages[-2]
    elif change == "unknown":
        request.messages[-1].call_id = "unknown"
    elif change == "mixed":
        request.messages.insert(-1, UserMessage("interrupt"))
    else:
        request.messages[-1].name = "wrong"
    with pytest.raises(UnsupportedCapabilityError):
        to_sampling(request, 100)


@pytest.mark.parametrize(
    "params",
    [
        {"metadata": {"api_key": "never-forward"}},
        {"includeContext": "allServers"},
        {"stopSequences": ["end"]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "audio", "data": "AA==", "mimeType": "audio/wav"},
                }
            ]
        },
        {"tools": [{"name": "x", "inputSchema": {}, "outputSchema": {"type": "object"}}]},
    ],
)
async def test_unsupported_sampling_inputs_rejected_before_provider(params):
    provider = EchoProvider()
    with pytest.raises(UnsupportedCapabilityError):
        await SamplingHandler(provider, model="host")(
            CONTEXT, sampling_params(**params), CancelToken()
        )
    assert not provider.requests


@pytest.mark.parametrize(
    "model_request",
    [
        ModelRequest([UserMessage("x")], options={"thinking_level": "high"}),
        ModelRequest([UserMessage("x")], api_key="secret"),
        ModelRequest([AssistantMessage([ThinkingContent("reasoning")])]),
        ModelRequest([AssistantMessage([ToolCall("a", "x", {}, thought_signature="opaque")])]),
        ModelRequest([CustomMessage("business", {})]),
    ],
)
def test_server_provider_rejects_unrepresentable_content_and_options(model_request):
    with pytest.raises(UnsupportedCapabilityError):
        to_sampling(model_request, 100)


async def test_sampling_host_controls_model_budget_and_authorization():
    provider = EchoProvider()
    handler = SamplingHandler(provider, model="host", max_tokens=10, options={"top_p": 0.5})
    params = sampling_params(modelPreferences={"hints": [{"name": "server-choice"}]})
    result = await handler(CONTEXT, params, CancelToken())
    assert result.model == "host"
    assert provider.requests[0].options == {"max_tokens": 10, "top_p": 0.5}

    async def deny(context, request):
        assert context.server == "business" and request.model == "host"
        return False

    with pytest.raises(PermissionError):
        await SamplingHandler(provider, model="host", authorize=deny)(
            CONTEXT, params, CancelToken()
        )
    assert len(provider.requests) == 1


async def test_sampling_provider_errors_and_token_cancellation():
    with pytest.raises(SamplingFailure, match="provider"):
        await SamplingHandler(ScriptedProvider([RuntimeError("model failed")]), model="host")(
            CONTEXT,
            sampling_params(),
            CancelToken(),
        )
    started, stopped = asyncio.Event(), asyncio.Event()

    class Waiting:
        async def stream(self, request, cancel):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            yield ModelEvent.done(AssistantMessage.text("never"))

    cancel = CancelToken()
    task = asyncio.create_task(
        SamplingHandler(Waiting(), model="host")(CONTEXT, sampling_params(), cancel)
    )
    await started.wait()
    cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
async def test_elicitation_actions(action):
    content = {"text": "ok", "choice": "b", "confirm": False} if action == "accept" else None

    async def ui(request, cancel):
        assert isinstance(request, ElicitationRequest)
        assert request.context == CONTEXT
        assert request.schema["properties"]["choice"]["enum"] == ["a", "b"]
        assert "Choose" not in repr(request)
        return ElicitationResponse(action, content)

    response = await ElicitationHandler(ui)(CONTEXT, form_params(), CancelToken())
    assert response.action == action and response.content == content


@pytest.mark.parametrize(
    "response",
    [
        ElicitationResponse("accept", {"text": "x", "choice": "a", "confirm": True}),
        ElicitationResponse("accept", {"text": "ok", "choice": "z", "confirm": True}),
        ElicitationResponse("accept", {"text": "ok", "choice": "a", "confirm": "yes"}),
        ElicitationResponse("accept", {}),
        ElicitationResponse("decline", {"text": "must not leak"}),
    ],
)
async def test_invalid_form_response_rejected(response):
    async def ui(request, cancel):
        return response

    with pytest.raises(ValueError):
        await ElicitationHandler(ui)(CONTEXT, form_params(), CancelToken())


async def test_form_timeout_url_and_cancel():
    stopped = asyncio.Event()

    async def wait(request, cancel):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(TimeoutError):
        await ElicitationHandler(wait, timeout=0.01)(CONTEXT, form_params(), CancelToken())
    assert stopped.is_set()
    with pytest.raises(UnsupportedCapabilityError):
        await ElicitationHandler(wait)(
            CONTEXT,
            types.ElicitRequestURLParams(
                message="sign in", url="https://example.org", elicitation_id="id"
            ),
            CancelToken(),
        )
    token = CancelToken()
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ElicitationHandler(wait)(CONTEXT, form_params(), token)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"api_key": {"type": "string"}}},
        {"type": "object", "properties": {"field": {"type": "string", "format": "password"}}},
        {"type": "object", "properties": {"field": {"type": "object"}}},
        {"type": "object", "properties": {"field": {"$ref": "https://example.org/schema"}}},
        {
            "type": "object",
            "properties": {"field": {"type": "string", "pattern": "^(a+)+$"}},
        },
        {
            "type": "object",
            "properties": {"field": {"type": "string", "pattern": "^[a-z]+$"}},
        },
    ],
)
async def test_form_rejects_sensitive_and_unsupported_schema_before_ui(schema):
    async def ui(request, cancel):
        pytest.fail("invalid form reached UI")

    with pytest.raises(UnsupportedCapabilityError):
        await ElicitationHandler(ui)(CONTEXT, form_params(requestedSchema=schema), CancelToken())


@pytest.mark.parametrize(
    "format_name,value",
    [
        ("email", "not-email"),
        ("date", "2026-99-99"),
        ("date-time", "not-a-date"),
        ("uri", "not a URI"),
    ],
)
async def test_form_formats_are_validated_or_explicitly_unsupported(format_name, value):
    from jsonschema import FormatChecker

    async def ui(request, cancel):
        return ElicitationResponse("accept", {"field": value})

    params = form_params(
        requestedSchema={
            "type": "object",
            "properties": {
                "field": {"type": "string", "format": format_name},
            },
            "required": ["field"],
        }
    )
    error = ValueError if format_name in FormatChecker().checkers else UnsupportedCapabilityError
    with pytest.raises(error):
        await ElicitationHandler(ui)(CONTEXT, params, CancelToken())


async def test_missing_form_format_validator_is_not_silently_ignored(monkeypatch):
    from jsonschema import FormatChecker

    monkeypatch.delitem(FormatChecker.checkers, "date")
    params = form_params(
        requestedSchema={
            "type": "object",
            "properties": {
                "field": {"type": "string", "format": "date"},
            },
        }
    )
    with pytest.raises(UnsupportedCapabilityError, match="format-nongpl"):
        await ElicitationHandler(choose)(CONTEXT, params, CancelToken())


async def test_protocol_mismatch_is_rejected_and_form_only_is_advertised(monkeypatch):
    from mcp import ClientSession
    from pi_python._mcp_interaction import _Interaction

    async def send(self, request, result_type, **kwargs):
        assert request.params.protocol_version == "2025-11-25"
        caps = request.params.capabilities.model_dump(by_alias=True, exclude_none=True)
        assert caps == {
            "sampling": {},
            "elicitation": {"form": {}},
            "experimental": {"io.pi-python/sampling-v1": {}, "io.pi-python/sampling-delta-v1": {}},
        }
        return types.InitializeResult(
            protocol_version="2025-06-18",
            capabilities=types.ServerCapabilities(),
            server_info=types.Implementation(name="old", version="1"),
        )

    monkeypatch.setattr(ClientSession, "send_request", send)
    interaction = _Interaction(
        MCPCallbacks(
            sampling=SamplingHandler(EchoProvider(), model="host", allow_tools=False),
            elicitation=ElicitationHandler(choose),
        ),
        "server",
        None,
    )
    with pytest.raises(ConfigurationError, match="protocol 2025-11-25"):
        await interaction.session(object(), object()).initialize()


async def test_tool_sampling_capability_and_images_are_opt_in():
    provider = EchoProvider()
    params = sampling_params(tools=[{"name": "x", "inputSchema": {"type": "object"}}])
    with pytest.raises(UnsupportedCapabilityError, match="tools are not enabled"):
        await SamplingHandler(provider, model="host", allow_tools=False)(
            CONTEXT, params, CancelToken()
        )
    image = {"type": "image", "mimeType": "image/png", "data": "AAAA"}
    params = sampling_params(messages=[{"role": "user", "content": [image]}])
    with pytest.raises(UnsupportedCapabilityError, match="images are not enabled"):
        await SamplingHandler(provider, model="host")(CONTEXT, params, CancelToken())


async def test_ungranted_service_and_unknown_runtime_grants_fail():
    plugin = Plugin(
        "p", lambda api: api.add_mcp_server("s", {"command": "unused", "enabled": False})
    )
    with pytest.raises(ConfigurationError, match="unknown"):
        async with load_plugins(plugin, mcp_callbacks={("p", "other"): MCPCallbacks()}):
            pass


async def test_server_provider_scope_capabilities_and_terminal_event():
    calls = []

    class Session:
        client_capabilities = types.ClientCapabilities(
            sampling=types.SamplingCapability(tools=types.SamplingToolsCapability())
        )

        async def create_message(self, messages, **kwargs):
            calls.append(kwargs)
            return types.CreateMessageResult(
                role="assistant",
                content=types.TextContent(type="text", text="ok"),
                model="host",
                stop_reason="endTurn",
            )

    provider = SamplingProvider(NS(protocol_version="2025-11-25", request_id=23, session=Session()))
    async with provider:
        events = [
            e async for e in provider.stream(ModelRequest([UserMessage("hi")]), CancelToken())
        ]
        assert [e.type for e in events] == ["done"] and events[0].message.content[0].text == "ok"
        assert calls[0]["related_request_id"] == 23
        provider.context.session.client_capabilities = types.ClientCapabilities()
        with pytest.raises(UnsupportedCapabilityError, match="not enabled"):
            _ = [e async for e in provider.stream(ModelRequest([UserMessage("hi")]), CancelToken())]
    with pytest.raises(ConfigurationError, match="scope"):
        _ = [e async for e in provider.stream(ModelRequest([UserMessage("hi")]), CancelToken())]
    with pytest.raises(ConfigurationError, match="2025-11-25"):
        SamplingProvider(NS(protocol_version="2026-07-28", request_id=23))


async def test_plugin_grants_readiness_and_metadata(endpoint):
    def setup(api):
        api.add_mcp_server(
            "business",
            {
                **endpoint,
                "call_metadata": {"context_id": "retained"},
                "required_capabilities": ["sampling.tools", "elicitation.form"],
            },
        )

    plugin = Plugin("demo", setup)
    with pytest.raises(ConfigurationError, match="lacks host capabilities"):
        async with load_plugins(plugin):
            pass
    callbacks = MCPCallbacks(
        sampling=SamplingHandler(EchoProvider(), model="host"),
        elicitation=ElicitationHandler(choose),
    )
    async with load_plugins(
        plugin, strict=True, mcp_callbacks={("demo", "business"): callbacks}
    ) as plugins:
        status = await plugins.readiness(
            required_mcp_capabilities={"business": ["sampling.tools", "elicitation.form"]}
        )
        assert status.ready
        missing = await plugins.readiness(
            required_mcp_capabilities={"business": ["elicitation.url"]}
        )
        assert not missing.ready and missing.missing_mcp_capabilities == (
            ("business", "elicitation.url"),
        )
        workflow = next(t for t in plugins.tools if t.name.endswith("__workflow"))
        assert not (await workflow.execute({"label": "plugin"}, tool_context())).is_error
    assert plugins.mcp_capabilities == {}


@pytest.mark.parametrize("stage", ["sampling", "elicitation"])
async def test_callback_timeout_error_and_audit(endpoint, stage):
    events = []

    async def audit(event):
        events.append(event)

    class Slow:
        async def stream(self, request, cancel):
            await asyncio.sleep(10)
            yield ModelEvent.done(AssistantMessage.text("never"))

    async def ui(request, cancel):
        await asyncio.sleep(10)
        return ElicitationResponse("accept", {})

    callbacks = MCPCallbacks(
        sampling=SamplingHandler(Slow(), model="host", timeout=0.03),
        elicitation=ElicitationHandler(ui, timeout=0.03),
        on_event=audit,
    )
    async with connected(endpoint, callbacks) as tools:
        with pytest.raises(Exception, match="timed out"):
            await tools["raw_sample" if stage == "sampling" else "ask"].execute(
                {"label": "private-message"}, tool_context()
            )
        assert events[-1].status == "timeout" and events[-1].kind == stage
        assert "private-message" not in repr(events)


async def test_model_failure_is_sanitized_and_budget_enforced(endpoint):
    secret = "provider-api-key-must-not-leak"
    callbacks = MCPCallbacks(
        sampling=SamplingHandler(ScriptedProvider([RuntimeError(secret)]), model="host")
    )
    async with connected(endpoint, callbacks) as tools:
        with pytest.raises(Exception, match="model request failed") as caught:
            await tools["raw_sample"].execute({"label": "private"}, tool_context())
        assert secret not in str(caught.value)
        assert not (await tools["capabilities"].execute({}, tool_context())).is_error

    callbacks = MCPCallbacks(
        sampling=SamplingHandler(EchoProvider(), model="host"),
        elicitation=ElicitationHandler(choose),
        max_requests_per_call=1,
    )
    async with connected(endpoint, callbacks) as tools:
        result = await tools["workflow"].execute({"label": "budget"}, tool_context())
        assert result.is_error


async def test_agent_tool_timeout_cancels_nested_model(endpoint):
    started, stopped = asyncio.Event(), asyncio.Event()

    class Waiting:
        async def stream(self, request, cancel):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            yield ModelEvent.done(AssistantMessage.text("never"))

    async with connected(
        endpoint, MCPCallbacks(sampling=SamplingHandler(Waiting(), model="host"))
    ) as tools:
        agent = Agent(
            provider=ScriptedProvider(
                [
                    AssistantMessage(
                        [ToolCall("outer", "raw_sample", {"label": "wait"})], "tool_use"
                    ),
                    AssistantMessage.text("timed out"),
                ]
            ),
            tools=list(tools.values()),
            limits=RunLimits(tool_timeout=0.3, cleanup_timeout=2),
        )
        result = await agent.prompt("go")
        assert started.is_set() and stopped.is_set()
        assert result.tool_outcomes[0].result.is_error
        assert not (await tools["capabilities"].execute({}, tool_context())).is_error


@pytest.mark.parametrize("disconnect", [False, True])
async def test_close_or_disconnect_joins_pending_ui(endpoint, disconnect):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def ui(request, cancel):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = None
    try:
        async with connected(endpoint, MCPCallbacks(elicitation=ElicitationHandler(ui))) as tools:
            name = "disconnect_during_callback" if disconnect else "ask"
            task = asyncio.create_task(
                tools[name].execute({} if disconnect else {"label": "wait"}, tool_context())
            )
            await asyncio.wait_for(started.wait(), 5)
            if disconnect:
                with pytest.raises(Exception):
                    await asyncio.wait_for(task, 5)
                await asyncio.wait_for(stopped.wait(), 2)
        await asyncio.wait_for(stopped.wait(), 2)
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("mcp-") and not t.done()]


@pytest.mark.parametrize("idle", [False, True])
async def test_plugin_readiness_revokes_capabilities_after_disconnect(endpoint, idle):
    async def ui(request, cancel):
        await asyncio.Event().wait()

    def setup(api):
        api.add_mcp_server("business", endpoint)

    callbacks = MCPCallbacks(elicitation=ElicitationHandler(ui))
    async with load_plugins(
        Plugin("p", setup), mcp_callbacks={("p", "business"): callbacks}
    ) as plugins:
        requirements = {
            "required_mcp_servers": ["business"],
            "required_mcp_capabilities": {"business": ["elicitation.form"]},
        }
        assert (await plugins.readiness(**requirements)).ready
        if idle:
            tool = next(t for t in plugins.tools if t.name.endswith("__capabilities"))
            result = await tool.execute({}, tool_context())
            os.kill(json.loads(result.content[0].text)["pid"], signal.SIGTERM)
        else:
            tool = next(t for t in plugins.tools if t.name.endswith("__disconnect_during_callback"))
            with pytest.raises(Exception):
                await asyncio.wait_for(tool.execute({}, tool_context()), 5)
        async with asyncio.timeout(5):
            while (status := await plugins.readiness(**requirements)).ready:
                await asyncio.sleep(0.01)
        assert not status.ready
        assert status.missing_mcp_servers == ("business",)
        assert status.missing_mcp_capabilities == (("business", "elicitation.form"),)
        assert plugins.mcp_capabilities == {}
        with pytest.raises(ConfigurationError, match="MCP servers"):
            status.require_ready()


async def test_provider_gate_is_shared_across_connections(endpoint):
    active, peak = 0, 0

    class Serial(EchoProvider):
        async def stream(self, request, cancel):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.03)
                async for event in super().stream(request, cancel):
                    yield event
            finally:
                active -= 1

    handler = SamplingHandler(Serial(), model="host")
    async with connected(endpoint, MCPCallbacks(sampling=handler)) as first:
        async with connected(endpoint, MCPCallbacks(sampling=handler)) as second:
            results = await asyncio.gather(
                *[
                    tools["sample"].execute({"label": label}, tool_context(label))
                    for tools, label in ((first, "first"), (second, "second"))
                ]
            )
            assert [json.loads(r.content[0].text)["text"] for r in results] == ["first", "second"]
    assert peak == 1


def test_sampling_result_preserves_calls_and_rejects_reasoning():
    original = AssistantMessage(
        [TextContent("work"), ToolCall("a", "one", {}), ToolCall("b", "two", {})], "tool_use"
    )
    wire = to_sampling_result(original, True, "host")
    restored = from_sampling_result(wire.model_dump(by_alias=True, exclude_none=True))
    assert restored.content == original.content and restored.stop_reason == "tool_use"
    with pytest.raises(UnsupportedCapabilityError):
        to_sampling_result(AssistantMessage([ThinkingContent("private")]), True, "host")


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX process-group cleanup; Windows SDK uses Job Objects"
)
async def test_stdio_forced_shutdown_cleans_same_group_children():
    pids = None
    try:
        async with connected(
            {"command": sys.executable, "args": [str(SERVER)]}, MCPCallbacks()
        ) as tools:
            pids = json.loads(
                (await tools["stubborn_tree"].execute({}, tool_context())).content[0].text
            )
        for pid in pids.values():
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            ).stdout.strip()
            assert not state or state.startswith("Z"), f"process {pid} still running: {state}"
    finally:
        if pids:
            for pid in pids.values():
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
