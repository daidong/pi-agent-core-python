"""HTTP/SSE and WebSocket transport. Optional dependencies load only on use."""

from __future__ import annotations
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from typing import TypeVar
import asyncio
import codecs
import json
import hashlib
import time
from email.utils import parsedate_to_datetime
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from ..cancellation import CancelToken
from ..errors import PiError, ProviderProtocolError
import math
import re
from urllib.parse import quote
from ..tools import invoke


T = TypeVar("T")


MAX_ERROR_BODY_CHARS = 4000  # the same cap as Pi
# Header names that carry credentials: authorization, x-api-key, x-goog-api-key, cookie, ...
_CREDENTIAL_HEADER = re.compile(r"auth|key|token|secret|cookie|password", re.I)


class ProviderHTTPError(PiError):
    """Structured HTTP failure. As in Pi, a model request's error carries the server's
    response body, capped, so callers can recognize causes such as context overflow;
    credentials sent with the request are redacted from it."""

    def __init__(
        self,
        status: int,
        message: str = "Provider request failed",
        *,
        retry_after: float | None = None,
        request_id: str | None = None,
        body: str | None = None,
    ) -> None:
        self.status = status
        self.retry_after = retry_after
        self.request_id = request_id
        self.body = body
        self.retryable = status in {429, 500, 502, 503, 504}
        self.category = (
            "authentication"
            if status in {401, 403}
            else "rate_limit"
            if status == 429
            else "server"
            if status >= 500
            else "request"
        )
        detail = f": {body}" if body else ""
        super().__init__(f"{message} (HTTP {status}; {self.category}){detail}")


async def error_body(response: Any, headers: Mapping[str, str]) -> str | None:
    """Read a failed response's body (bounded), redact request credentials, cap its length."""
    data = b""
    try:
        async for chunk in response.aiter_bytes():
            data += chunk
            if len(data) > 4 * MAX_ERROR_BODY_CHARS:
                break
    except Exception:
        return None
    text = data.decode("utf-8", "replace").strip()
    for name, value in headers.items():
        if _CREDENTIAL_HEADER.search(name):
            for part in str(value).split():
                if len(part) >= 8:
                    # Also as echoed inside JSON ("/" escaped) or a URL (percent-encoded).
                    for form in {part, part.replace("/", "\\/"), quote(part, safe="")}:
                        text = text.replace(form, "[redacted]")
    if len(text) > MAX_ERROR_BODY_CHARS:
        text = (
            f"{text[:MAX_ERROR_BODY_CHARS]}... [truncated {len(text) - MAX_ERROR_BODY_CHARS} chars]"
        )
    return text or None


def retry_delay(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None


async def cancellable(awaitable: Awaitable[T], cancel: CancelToken) -> T:
    task = asyncio.ensure_future(awaitable)
    signal = asyncio.create_task(cancel.wait())
    try:
        cancel.raise_if_cancelled()
        done, _ = await asyncio.wait({task, signal}, return_when=asyncio.FIRST_COMPLETED)
        if signal in done:
            raise asyncio.CancelledError(cancel.reason)
        return task.result()
    finally:
        signal.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, signal, return_exceptions=True)


async def sse_events(chunks: AsyncIterator[bytes]) -> AsyncGenerator[dict[str, Any], None]:
    """SSE framing across UTF-8 chunks, CR/LF variants, multiline data and EOF."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    data: list[str] = []
    event_name = None

    async def emit() -> dict[str, Any] | None:
        nonlocal data, event_name
        if not data:
            event_name = None
            return None
        payload = "\n".join(data)
        data = []
        if payload == "[DONE]":
            event_name = None
            return {"type": "transport_done"}
        try:
            value = json.loads(payload)
        except ValueError as exc:
            raise ProviderProtocolError("Invalid SSE JSON") from exc
        if not isinstance(value, dict):
            raise ProviderProtocolError("SSE data must be an object")
        if "type" not in value and event_name:
            value["type"] = event_name
        event_name = None
        return value

    async def lines(final: bool = False) -> AsyncGenerator[dict[str, Any], None]:
        nonlocal buffer, event_name
        while True:
            positions = [p for p in (buffer.find("\n"), buffer.find("\r")) if p >= 0]
            if not positions:
                if not final or not buffer:
                    break
                line, buffer = buffer, ""
            else:
                pos = min(positions)
                if buffer[pos] == "\r" and pos + 1 == len(buffer) and not final:
                    break
                end = pos + 2 if buffer[pos : pos + 2] == "\r\n" else pos + 1
                line, buffer = buffer[:pos], buffer[end:]
            if not line:
                value = await emit()
                if value is not None:
                    yield value
            elif line.startswith("data:"):
                field = line[5:]
                data.append(field[1:] if field.startswith(" ") else field)
            elif line.startswith("event:"):
                event_name = line[6:].lstrip(" ")

    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        async for value in lines():
            yield value
    buffer += decoder.decode(b"", final=True)
    async for value in lines(final=True):
        yield value
    last = await emit()
    if last is not None:
        yield last


class HTTPTransport:
    def __init__(
        self,
        client: Any = None,
        *,
        timeout: float = 300,
        max_retries: int = 2,
        retry_base: float = 0.5,
        max_retry_delay: float = 30,
        websocket_idle_ttl: float = 5 * 60,
        websocket_max_age: float = 55 * 60,
    ) -> None:
        if type(max_retries) is not int or not 0 <= max_retries <= 10:
            raise ValueError("max_retries must be between 0 and 10")
        if retry_base < 0 or max_retry_delay < 0:
            raise ValueError("Retry delays must be nonnegative")
        self.client = client
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base = retry_base
        self.max_retry_delay = max_retry_delay
        self.websocket_idle_ttl = websocket_idle_ttl
        self.websocket_max_age = websocket_max_age
        self._clock = time.monotonic
        self._sockets: dict[tuple, CachedSocket] = {}
        self._socket_locks: dict[tuple, asyncio.Lock] = {}
        self._stats = dict(
            http_attempts=0,
            http_retries=0,
            websocket_connects=0,
            websocket_reuses=0,
            websocket_fallbacks=0,
            websocket_expired=0,
            websocket_retries=0,
            websocket_delta_requests=0,
        )

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    async def aclose(self) -> None:
        sockets, self._sockets = self._sockets, {}
        await asyncio.gather(*(entry.socket.close() for entry in sockets.values()))
        self._socket_locks.clear()

    async def __aenter__(self) -> HTTPTransport:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        if self.client is not None:
            yield self.client
        else:
            import httpx

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=30),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                yield client

    async def request_json(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        cancel: CancelToken,
        *,
        form: bool = False,
    ) -> dict[str, Any]:
        async with self._client() as client:
            response = await cancellable(
                client.post(url, headers=headers, **({"data": body} if form else {"json": body})),
                cancel,
            )
            if response.status_code >= 400:
                raise ProviderHTTPError(response.status_code)
            value = response.json()
            if not isinstance(value, dict):
                raise ProviderProtocolError("Expected JSON object")
            return value

    async def get_json(self, url: str, cancel: CancelToken) -> Any:
        async with self._client() as client:
            response = await cancellable(client.get(url), cancel)
            if response.status_code >= 400:
                raise ProviderHTTPError(response.status_code)
            return response.json()

    async def stream(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        cancel: CancelToken,
        *,
        on_response: Callable | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        async with self._client() as client:
            for attempt in range(self.max_retries + 1):
                request = client.build_request("POST", url, headers=headers, json=body)
                # A network exception may follow server acceptance. Never replay it.
                self._stats["http_attempts"] += 1
                response = await cancellable(client.send(request, stream=True), cancel)
                try:
                    await invoke(
                        on_response,
                        {"status": response.status_code, "headers": dict(response.headers)},
                    )
                    if response.status_code < 400:
                        break
                    error = ProviderHTTPError(
                        response.status_code,
                        retry_after=retry_delay(response.headers),
                        request_id=response.headers.get("x-request-id"),
                        body=await cancellable(error_body(response, headers), cancel),
                    )
                except BaseException:
                    await response.aclose()
                    raise
                await response.aclose()
                if not error.retryable or attempt == self.max_retries:
                    raise error
                delay = (
                    error.retry_after
                    if error.retry_after is not None
                    else self.retry_base * 2**attempt
                )
                # Do not retry sooner than a server's requested delay.
                if delay > self.max_retry_delay:
                    raise error
                self._stats["http_retries"] += 1
                await cancellable(asyncio.sleep(delay), cancel)
            try:
                iterator = sse_events(response.aiter_bytes())
                while True:
                    try:
                        event = await cancellable(anext(iterator), cancel)
                    except StopAsyncIteration:
                        break
                    yield event
            finally:
                await response.aclose()

    async def responses(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        cancel: CancelToken,
        *,
        mode: str = "sse",
        session_id: str | None = None,
        on_response: Callable | None = None,
        link: WebSocketLink | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        if mode == "sse":
            events = self.stream(url, body, headers, cancel, on_response=on_response)
        else:
            events = self.websocket(
                url,
                body,
                headers,
                cancel,
                on_response=on_response,
                session_id=session_id if mode in {"websocket-cached", "auto"} else None,
                fallback=mode == "auto",
                link=link,
            )
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    def _reusable(self, entry: CachedSocket) -> bool:
        """Pi acquireWebSocket: reuse only an open, young socket that was not idle too long."""
        now = self._clock()
        state = getattr(entry.socket, "state", None)
        open_ = state is None or getattr(state, "name", "OPEN") == "OPEN"
        return (
            open_
            and now - entry.created < self.websocket_max_age
            and now - entry.released < self.websocket_idle_ttl
        )

    async def websocket(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        cancel: CancelToken,
        *,
        on_response: Callable | None = None,
        session_id: str | None = None,
        fallback: bool = False,
        link: WebSocketLink | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidHandshake
        from websockets.version import version as websockets_version

        # websockets 15 started reading proxy settings from the environment; turn that
        # off. Older releases never use a proxy and do not accept the argument.
        no_proxy: dict[str, Any] = (
            {"proxy": None} if int(websockets_version.split(".")[0]) >= 15 else {}
        )

        endpoint = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        # Include all headers to prevent reuse across accounts or incompatible options.
        key = (
            endpoint,
            session_id,
            hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).hexdigest(),
        )
        lock = self._socket_locks.setdefault(key, asyncio.Lock()) if session_id else asyncio.Lock()
        await cancellable(lock.acquire(), cancel)
        entry: CachedSocket | None = None
        complete = False
        retried: set[str] = set()
        try:
            while True:
                entry = self._sockets.pop(key, None) if session_id else None
                if entry is not None and not self._reusable(entry):
                    self._stats["websocket_expired"] += 1
                    await entry.socket.close()
                    entry = None
                if entry is not None:
                    self._stats["websocket_reuses"] += 1
                else:
                    try:
                        socket = await cancellable(
                            connect(
                                endpoint,
                                additional_headers={
                                    **headers,
                                    "OpenAI-Beta": "responses_websockets=2026-02-06",
                                },
                                open_timeout=30,
                                max_size=16 * 1024 * 1024,
                                **no_proxy,
                            ),
                            cancel,
                        )
                        self._stats["websocket_connects"] += 1
                    except (
                        OSError,
                        TimeoutError,
                        InvalidHandshake,
                    ):
                        if not fallback:
                            raise
                        self._stats["websocket_fallbacks"] += 1
                        # No response.create has been sent, so SSE fallback is safe.
                        events = self.stream(url, body, headers, cancel, on_response=on_response)
                        try:
                            async for event in events:
                                yield event
                        finally:
                            await events.aclose()
                        return
                    entry = CachedSocket(socket, self._clock(), self._clock())
                await invoke(
                    on_response,
                    {"status": 101, "headers": dict(entry.socket.response.headers.raw_items())},
                )
                # A continuation is consumed here; the provider restores one only after a
                # complete response, so any failure falls back to the full context.
                state, entry.continuation = entry.continuation, None
                sent = continued_body(body, state) if link is not None else None
                if sent is not None:
                    self._stats["websocket_delta_requests"] += 1
                if link is not None:
                    link.entry = entry
                await cancellable(
                    entry.socket.send(
                        json.dumps(
                            {
                                "type": "response.create",
                                **{k: v for k, v in (sent or body).items() if k != "stream"},
                            }
                        )
                    ),
                    cancel,
                )
                output = False
                retry = None
                while True:
                    raw = await cancellable(entry.socket.recv(), cancel)
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise ProviderProtocolError("WebSocket event must be an object")
                    code = error_code(event)
                    # Rejected before any model output: Pi retries once with the full
                    # context on a fresh connection. Nothing was generated, so this
                    # cannot duplicate a response.
                    if not output and code in _RETRYABLE_WEBSOCKET_CODES and code not in retried:
                        if code != "previous_response_not_found" or sent is not None:
                            retry = code
                            break
                    output = output or str(event.get("type", "")).startswith(
                        ("response.output", "response.content_part", "response.reasoning")
                    )
                    terminal = event.get("type") in {
                        "response.completed",
                        "response.done",
                        "response.failed",
                        "response.incomplete",
                        "error",
                    }
                    # A response cut at the output limit still ends cleanly (Pi keeps it).
                    complete = event.get("type") in {
                        "response.completed",
                        "response.done",
                        "response.incomplete",
                    }
                    yield event
                    if terminal:
                        break
                if retry is None:
                    break
                retried.add(retry)
                self._stats["websocket_retries"] += 1
                await entry.socket.close()
                entry = None
        finally:
            if entry is not None:
                if session_id and complete and not cancel.cancelled:
                    entry.released = self._clock()
                    self._sockets[key] = entry
                else:
                    await entry.socket.close()
            lock.release()


_RETRYABLE_WEBSOCKET_CODES = {"previous_response_not_found", "websocket_connection_limit_reached"}


def error_code(event: dict) -> str | None:
    """Pi extractCodexEventError, plus the failed-response code."""

    def field(value: dict, name: str) -> dict:
        nested = value.get(name)
        return nested if isinstance(nested, dict) else {}

    if event.get("type") == "error":
        code = event.get("code", field(event, "error").get("code"))
    elif event.get("type") == "response.failed":
        code = field(field(event, "response"), "error").get("code")
    else:
        code = None
    return code if isinstance(code, str) else None


@dataclass
class CachedSocket:
    socket: Any
    created: float
    released: float
    continuation: dict | None = None


@dataclass
class WebSocketLink:
    """Lets a provider record the response items that the next request will repeat."""

    entry: CachedSocket | None = None


def continued_body(body: dict, state: dict | None) -> dict | None:
    """Pi buildCachedWebSocketRequestBody: send only new input after the last response.

    Valid only when every other field is unchanged and the new input starts with the
    previous input followed by the previous response's own items.
    """
    if not state or not state.get("response_id"):
        return None
    last = state["body"]
    if {k: v for k, v in body.items() if k not in {"input", "previous_response_id"}} != {
        k: v for k, v in last.items() if k not in {"input", "previous_response_id"}
    }:
        return None
    baseline = [*last.get("input", []), *state["items"]]
    current = body.get("input", [])
    if len(current) < len(baseline) or current[: len(baseline)] != baseline:
        return None
    return {**body, "previous_response_id": state["response_id"], "input": current[len(baseline) :]}
