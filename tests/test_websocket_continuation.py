"""Codex cached WebSocket: previous_response_id deltas, one-shot retries and expiry."""

import base64
import json

from pi_python import *
from pi_python.providers import HTTPTransport, OpenAICodexProvider

CLAIMS = {"https://api.openai.com/auth": {"chatgpt_account_id": "account"}}
TOKEN = "x." + base64.urlsafe_b64encode(json.dumps(CLAIMS).encode()).decode().rstrip("=") + ".y"
SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    "required": ["a", "b"],
    "additionalProperties": False,
}


def call_turn(response_id):
    item = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "add"}
    done = {**item, "arguments": '{"a":2,"b":3}'}
    return [
        {"type": "response.created", "response": {"id": response_id}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": '{"a":2,"b":3}',
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {"id": response_id, "status": "completed", "output": [done], "usage": {}},
        },
    ]


def text_turn(response_id, text="5"):
    item = {"type": "message", "id": f"msg_{response_id}", "role": "assistant", "content": []}
    done = {**item, "content": [{"type": "output_text", "text": text, "annotations": []}]}
    return [
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_text.delta", "output_index": 0, "delta": text},
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {"id": response_id, "status": "completed", "output": [done], "usage": {}},
        },
    ]


class Server:
    """A local Responses WebSocket endpoint scripted per request."""

    def __init__(self, script):
        self.script = script
        self.requests = []
        self.connections = 0

    async def handle(self, socket):
        self.connections += 1
        connection = self.connections
        async for raw in socket:
            request = json.loads(raw)
            self.requests.append((connection, request))
            reply = self.script(len(self.requests), request)
            if reply == "close":
                await socket.close()
                return
            for event in reply:
                await socket.send(json.dumps(event))

    async def __aenter__(self):
        from websockets.asyncio.server import serve

        self.server = await serve(self.handle, "127.0.0.1", 0).__aenter__()
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *args):
        await self.server.__aexit__(*args)


async def add(args, context):
    return ToolResult.text(str(args["a"] + args["b"]))


def agent(server, transport, **options):
    provider = OpenAICodexProvider(api_key=TOKEN, base_url=server.url, transport=transport)
    return Agent(
        provider=provider,
        model="gpt-5.5",
        system_prompt="Use the tool.",
        tools=[Tool("add", "Add", SCHEMA, add)],
        transport="websocket-cached",
        session_id="session",
        options=options,
    )


async def test_cached_websocket_sends_only_new_input_after_each_response():
    def script(n, request):
        return call_turn("resp_1") if n == 1 else text_turn(f"resp_{n}")

    async with Server(script) as server, HTTPTransport() as transport:
        bot = agent(server, transport)
        first = await bot.prompt("add 2 and 3")
        second = await bot.prompt("again")
    assert first.status == second.status == "completed"
    bodies = [request for _, request in server.requests]
    assert server.connections == 1 and len(bodies) == 3
    assert "previous_response_id" not in bodies[0]
    assert bodies[1]["previous_response_id"] == "resp_1"
    assert bodies[1]["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "5"}
    ]
    assert bodies[2]["previous_response_id"] == "resp_2"
    assert bodies[2]["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "again"}]}
    ]

    # Every field except the input is identical to the full request.
    def strip(body):
        return {k: v for k, v in body.items() if k not in {"input", "previous_response_id"}}

    assert strip(bodies[1]) == strip(bodies[0])
    assert transport.stats["websocket_delta_requests"] == 2


async def test_changed_options_or_missing_response_state_resend_full_context():
    def script(n, request):
        if n == 3 and "previous_response_id" in request:
            return [{"type": "error", "error": {"code": "previous_response_not_found"}}]
        return call_turn("resp_1") if n == 1 else text_turn(f"resp_{n}")

    async with Server(script) as server, HTTPTransport() as transport:
        bot = agent(server, transport)
        await bot.prompt("add 2 and 3")
        result = await bot.prompt("again")
        options = {"transport": "websocket-cached", "session_id": "session", "reasoning": "high"}
        bot.update_config(AgentConfigUpdate(options=options))
        await bot.prompt("changed")
    assert result.status == "completed"
    connections = [c for c, _ in server.requests]
    bodies = [request for _, request in server.requests]
    # Request 3 lost its server state: one retry with the full context on a new socket.
    assert connections == [1, 1, 1, 2, 2]
    assert bodies[2]["previous_response_id"] == "resp_2"
    assert "previous_response_id" not in bodies[3]
    assert bodies[3]["input"][0]["content"][0]["text"] == "add 2 and 3"
    # A different reasoning setting changes the body, so the context is sent in full.
    assert "previous_response_id" not in bodies[4] and bodies[4]["reasoning"]["effort"] == "high"
    assert transport.stats["websocket_retries"] == 1


async def test_connection_limit_before_output_retries_once():
    def script(n, request):
        if n in {1, 2}:
            return [{"type": "error", "code": "websocket_connection_limit_reached"}]
        return text_turn("resp_3")

    async with Server(script) as server, HTTPTransport() as transport:
        result = await agent(server, transport).prompt("hi")
    # The first refusal is retried once; the second is reported, not retried again.
    assert result.status == "failed" and server.connections == 2

    async with Server(lambda n, r: script(n + 1, r)) as server, HTTPTransport() as transport:
        result = await agent(server, transport).prompt("hi")
    assert result.status == "completed" and server.connections == 2


async def test_idle_aged_or_closed_sockets_are_replaced_before_reuse():
    now = [0.0]

    async with Server(lambda n, r: text_turn(f"resp_{n}")) as server:
        transport = HTTPTransport()
        transport._clock = lambda: now[0]
        bot = agent(server, transport)
        await bot.prompt("one")
        now[0] += 4 * 60  # still within the idle window
        await bot.prompt("two")
        now[0] += 5 * 60  # idle for five minutes
        await bot.prompt("three")
        for _ in range(12):  # active, but the connection reaches 55 minutes
            now[0] += 4.9 * 60
            await bot.prompt("busy")
        await transport.aclose()
    assert [c for c, _ in server.requests][:3] == [1, 1, 2]
    assert server.connections == 3 and transport.stats["websocket_expired"] == 2
    # A replaced socket carries no response state, so its first request is complete.
    assert "previous_response_id" not in server.requests[2][1]

    def closing(n, request):
        return "close" if n == 2 else text_turn(f"resp_{n}")

    async with Server(closing) as server, HTTPTransport() as transport:
        bot = agent(server, transport)
        await bot.prompt("one")
        socket = next(iter(transport._sockets.values())).socket
        await socket.send("{}")  # the server closes on this message
        await socket.wait_closed()
        result = await bot.prompt("two")
    assert result.status == "completed" and server.connections == 2


async def test_cache_retention_none_uses_one_shot_sockets():
    async with Server(lambda n, r: text_turn(f"resp_{n}")) as server, HTTPTransport() as transport:
        bot = agent(server, transport, cache_retention="none")
        await bot.prompt("one")
        await bot.prompt("two")
        assert not transport._sockets
    assert server.connections == 2
    assert all("previous_response_id" not in r for _, r in server.requests)


async def test_cancellation_at_lock_handoff_does_not_block_the_session():
    import asyncio
    import hashlib
    import pytest

    async with Server(lambda n, r: text_turn(f"resp_{n}")) as server, HTTPTransport() as transport:
        headers = {}
        key = (
            server.url.replace("http://", "ws://"),
            "race",
            hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).hexdigest(),
        )
        lock = asyncio.Lock()
        await lock.acquire()
        transport._socket_locks[key] = lock
        cancel = CancelToken()
        stream = transport.websocket(server.url, {}, headers, cancel, session_id="race")
        waiter = asyncio.create_task(anext(stream))
        for _ in range(10):
            await asyncio.sleep(0)
        lock.release()
        cancel.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await stream.aclose()
        assert not lock.locked()
        async with asyncio.timeout(2):
            events = [
                e
                async for e in transport.websocket(
                    server.url, {}, headers, CancelToken(), session_id="race"
                )
            ]
        assert events[-1]["type"] == "response.completed"


async def test_new_sessions_reclaim_expired_cached_connections():
    now = [0.0]
    async with (
        Server(lambda n, r: text_turn(f"resp_{n}")) as server,
        HTTPTransport(
            websocket_idle_ttl=1,
        ) as transport,
    ):
        transport._clock = lambda: now[0]
        previous = None
        for index in range(5):
            events = [
                e
                async for e in transport.websocket(
                    server.url, {}, {}, CancelToken(), session_id=str(index)
                )
            ]
            assert events[-1]["type"] == "response.completed"
            if previous is not None:
                assert previous.state.name == "CLOSED"
            assert len(transport._sockets) == 1
            assert not transport._socket_locks
            previous = next(iter(transport._sockets.values())).socket
            now[0] += 2


async def test_websocket_cache_capacity_evicts_oldest_idle_connection():
    async with (
        Server(lambda n, r: text_turn(f"resp_{n}")) as server,
        HTTPTransport(
            websocket_cache_size=2,
        ) as transport,
    ):
        sockets = []
        for index in range(3):
            async for _ in transport.websocket(
                server.url, {}, {}, CancelToken(), session_id=str(index)
            ):
                pass
            sockets.append(list(transport._sockets.values())[-1].socket)
        assert len(transport._sockets) == 2
        assert sockets[0].state.name == "CLOSED"
        assert all(s.state.name == "OPEN" for s in sockets[1:])


async def test_cancelled_waiter_keeps_the_lock_for_other_requests():
    import asyncio
    import pytest

    async with Server(lambda n, r: text_turn(f"resp_{n}")) as server, HTTPTransport() as transport:
        first = transport.websocket(server.url, {}, {}, CancelToken(), session_id="shared")
        await anext(first)  # Keep the response stream open, holding the session lock.
        cancelled = CancelToken()

        async def consume(token):
            return [
                e async for e in transport.websocket(server.url, {}, {}, token, session_id="shared")
            ]

        second = asyncio.create_task(consume(cancelled))
        third = asyncio.create_task(consume(CancelToken()))
        try:
            async with asyncio.timeout(2):
                while sum(transport._socket_users.values()) != 3:
                    await asyncio.sleep(0)
                cancelled.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await second
                assert not third.done()
                assert server.connections == 1
                await first.aclose()
                result = await third
            assert result[-1]["type"] == "response.completed"
            assert not transport._socket_locks
        finally:
            await first.aclose()
            for task in (second, third):
                task.cancel()
            await asyncio.gather(second, third, return_exceptions=True)


async def test_transport_close_does_not_recache_an_inflight_response():
    async with Server(lambda n, r: [{"type": "response.completed"}]) as server:
        transport = HTTPTransport()
        stream = transport.websocket(server.url, {}, {}, CancelToken(), session_id="active")
        await anext(stream)
        await transport.aclose()
        await stream.aclose()
        assert not transport._sockets
        assert not transport._socket_locks


async def test_zero_websocket_cache_size_disables_retention():
    async with (
        Server(lambda n, r: text_turn(f"resp_{n}")) as server,
        HTTPTransport(
            websocket_cache_size=0,
        ) as transport,
    ):
        for _ in range(2):
            async for _ in transport.websocket(server.url, {}, {}, CancelToken(), session_id="s"):
                pass
            assert not transport._sockets
        assert server.connections == 2
