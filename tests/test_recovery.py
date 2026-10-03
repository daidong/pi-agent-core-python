import asyncio
import json
import httpx
import pytest
from pi_python import CancelToken
from pi_python.providers import HTTPTransport
from pi_python.providers.transport import ProviderHTTPError, retry_delay


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_status_retry_before_stream_with_observed_attempts(status):
    requests = []
    observed = []

    def respond(request):
        requests.append(request)
        return (
            httpx.Response(status, headers={"Retry-After": "0"})
            if len(requests) == 1
            else httpx.Response(200, content='data: {"type":"done"}\n\n')
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = HTTPTransport(client, max_retries=1)
        events = [
            e
            async for e in transport.stream(
                "https://fixture.test",
                {},
                {},
                CancelToken(),
                on_response=lambda r: observed.append(r["status"]),
            )
        ]
    assert events == [{"type": "done"}] and observed == [status, 200] and len(requests) == 2


async def test_structured_error_no_auth_retry_and_retry_after_cap():
    for status, headers in [(401, {}), (429, {"Retry-After": "3600", "x-request-id": "request"})]:
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(status, headers=headers, content="body echoes key-0123456789")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(ProviderHTTPError) as caught:
                async for _ in HTTPTransport(client).stream(
                    "https://fixture.test", {}, {"x-api-key": "key-0123456789"}, CancelToken()
                ):
                    pass
        assert len(calls) == 1 and str(caught.value).endswith("body echoes [redacted]")
        assert caught.value.category == ("authentication" if status == 401 else "rate_limit")
        assert caught.value.retry_after == (3600 if status == 429 else None)


async def test_no_retry_on_unknown_network_outcome_or_partial_stream():
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"first"}\n\n'
            raise httpx.ReadError("disconnected after acceptance")

    for partial in (False, True):
        calls = []

        def respond(request):
            calls.append(request)
            if not partial:
                raise httpx.ReadError("unknown acceptance")
            return httpx.Response(200, stream=Interrupted())

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(httpx.ReadError):
                async for _ in HTTPTransport(client).stream(
                    "https://fixture.test", {}, {}, CancelToken()
                ):
                    pass
        assert len(calls) == 1


async def test_cancellation_interrupts_retry_sleep():
    token = CancelToken()
    ready = asyncio.Event()
    calls = []

    def respond(request):
        calls.append(request)
        ready.set()
        return httpx.Response(429, headers={"Retry-After": "20"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:

        async def run():
            return [
                e async for e in HTTPTransport(client).stream("https://fixture.test", {}, {}, token)
            ]

        task = asyncio.create_task(run())
        await ready.wait()
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    assert len(calls) == 1


async def test_auto_falls_back_only_before_websocket_request_sent(monkeypatch):
    from websockets.asyncio import client as ws

    calls = []

    async def unavailable(*args, **kwargs):
        raise OSError("handshake refused")

    monkeypatch.setattr(ws, "connect", unavailable)

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content='data: {"type":"response.completed"}\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = [
            e
            async for e in HTTPTransport(client).responses(
                "https://fixture.test", {}, {}, CancelToken(), mode="auto"
            )
        ]
    assert result == [{"type": "response.completed"}] and len(calls) == 1

    class Accepted:
        from websockets.datastructures import Headers

        response = type("Response", (), {"headers": Headers()})()

        async def send(self, raw):
            calls.append("sent")

        async def recv(self):
            raise OSError("lost after send")

        async def close(self):
            calls.append("closed")

    async def connect(*args, **kwargs):
        return Accepted()

    monkeypatch.setattr(ws, "connect", connect)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OSError):
            async for _ in HTTPTransport(client).responses(
                "https://fixture.test", {}, {}, CancelToken(), mode="auto"
            ):
                pass
    assert len(calls) == 3 and calls[-2:] == ["sent", "closed"]


async def test_cached_websocket_reuses_same_session_isolates_credentials_and_closes():
    from websockets.asyncio.server import serve

    connections = []
    requests = []

    async def handle(socket):
        connections.append(socket)
        async for raw in socket:
            requests.append(json.loads(raw))
            await socket.send(json.dumps({"type": "response.completed"}))

    def headers(connection, request, response):
        response.headers["set-cookie"] = "one=1"
        response.headers["set-cookie"] = "two=2"
        return response

    async with serve(handle, "127.0.0.1", 0, process_response=headers) as server:
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/responses"
        async with HTTPTransport() as transport:
            for credential in ["one", "one", "two"]:
                events = [
                    e
                    async for e in transport.responses(
                        url,
                        {"stream": True, "input": []},
                        {"authorization": credential},
                        CancelToken(),
                        mode="websocket-cached",
                        session_id="session",
                    )
                ]
                assert events == [{"type": "response.completed"}]
            assert len(connections) == 2 and len(requests) == 3
            assert len(transport._sockets) == 2
        assert not transport._sockets
    assert all(r["type"] == "response.create" and "stream" not in r for r in requests)


def test_retry_after_parsing():
    assert retry_delay({"retry-after": "NaN"}) is None
    assert retry_delay({"retry-after": "bad"}) is None
    assert retry_delay({"retry-after": "-1"}) == 0
