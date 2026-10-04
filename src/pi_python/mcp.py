"""Use the tools of an MCP server as Agent tools.

`mcp_tools(session)` wraps every tool of a connected session from the official `mcp` SDK
(1.x or 2.x); `connect_stdio(...)` also starts a stdio server and closes it afterwards, and
`connect_http(...)` connects to a streamable HTTP server.
The conversion follows Pi's MCP adapter: text and images pass through, embedded text and
image resources are unwrapped, other blocks become short text placeholders, and MCP's
`isError` marks a failed call. Install with ``pip install 'pi-python-core[mcp]'``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import warnings
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from .errors import ConfigurationError
from .messages import ImageContent, TextContent
from .tools import Tool, ToolContext, ToolResult


def _get(value: Any, snake: str, camel: str | None = None) -> Any:
    """Read a field in either SDK's naming (2.x snake_case, 1.x camelCase) or from a dict."""
    for name in (snake, camel) if camel else (snake,):
        if isinstance(value, Mapping):
            if name in value:
                return value[name]
        elif hasattr(value, name):
            return getattr(value, name)
    return None


def _block(block: Any) -> TextContent | ImageContent:
    kind = _get(block, "type")
    if kind == "text":
        return TextContent(_get(block, "text") or "")
    if kind == "image":
        return ImageContent(_get(block, "data"), _get(block, "mime_type", "mimeType"))
    if kind == "audio":
        return TextContent(f"[audio {_get(block, 'mime_type', 'mimeType')} omitted]")
    if kind == "resource_link":
        return TextContent(f"{_get(block, 'name')}: {_get(block, 'uri')}")
    if kind == "resource":
        resource = _get(block, "resource")
        mime = _get(resource, "mime_type", "mimeType")
        if _get(resource, "text") is not None:
            return TextContent(_get(resource, "text"))
        if mime and str(mime).startswith("image/"):
            return ImageContent(_get(resource, "blob"), mime)
        return TextContent(
            f"[binary resource {_get(resource, 'uri')} ({mime or 'unknown type'}) omitted]"
        )
    return TextContent(f"[unsupported MCP content {kind}]")


def _tool_name(prefix: str | None, name: str, taken: set[str]) -> str:
    # Model APIs accept letters, digits, "_" and "-", up to 64 characters.
    full = f"{prefix}_{name}" if prefix else name
    result = re.sub(r"[^A-Za-z0-9_-]", "_", full)[:64]
    if result in taken:  # "get.file" and "get_file", or two long names with one prefix
        result = f"{result[:55]}_{hashlib.sha1(full.encode()).hexdigest()[:8]}"
        warnings.warn(f"MCP tool {name!r} renamed to {result!r} to keep names unique", stacklevel=3)
    taken.add(result)
    return result


def _wrap(session: Any, spec: Any, prefix: str | None, taken: set[str]) -> Tool:
    name = _get(spec, "name")
    schema = dict(_get(spec, "input_schema", "inputSchema") or {})
    # Model APIs require an object schema, and some reject one without `properties`.
    schema.update(type="object", properties=schema.get("properties") or {})

    async def execute(args: dict[str, Any], context: ToolContext) -> ToolResult:
        async def progress(
            done: float, total: float | None = None, message: str | None = None
        ) -> None:
            await context.emit_update({"progress": done, "total": total, "message": message})

        result = await session.call_tool(name, args, progress_callback=progress)
        blocks = _get(result, "content")
        if blocks is None:
            # MCP 2.x can ask the client for input mid-call; this adapter cannot answer.
            return ToolResult.text(
                f"MCP tool {name} asked for input this client cannot provide", is_error=True
            )
        content = [_block(b) for b in blocks]
        structured = _get(result, "structured_content", "structuredContent")
        is_error = _get(result, "is_error", "isError") is True
        if not content and structured is not None:
            content = [TextContent(json.dumps(structured, indent=2, ensure_ascii=False))]
        if is_error and not content:
            content = [TextContent(f"MCP tool {name} failed")]
        return ToolResult(content, structured_content=structured, is_error=is_error)

    description = _get(spec, "description") or _get(spec, "title") or f"MCP tool {name}"
    tool = Tool(_tool_name(prefix, name, set(taken)), description, schema, execute)
    taken.add(tool.name)
    return tool


async def mcp_tools(
    session: Any, *, prefix: str | None = None, names: Iterable[str] | None = None
) -> list[Tool]:
    """Wrap the tools of an initialized MCP client session.

    `prefix` namespaces the tool names (``prefix_tool``); `names` keeps only those MCP
    tools. A tool whose input schema cannot be used is skipped with a warning, so one
    unusual tool does not make the whole server unusable.
    """
    wanted = set(names) if names is not None else None
    specs: list[Any] = []
    cursor = None
    while True:
        page = await _list_page(session, cursor)
        specs.extend(_get(page, "tools") or [])
        cursor = _get(page, "next_cursor", "nextCursor")
        if not cursor:
            break
    tools: list[Tool] = []
    taken: set[str] = set()
    for spec in specs:
        if wanted is not None and _get(spec, "name") not in wanted:
            continue
        try:
            tools.append(_wrap(session, spec, prefix, taken))
        except (ConfigurationError, RecursionError) as exc:
            warnings.warn(f"Skipping MCP tool {_get(spec, 'name')}: {exc}", stacklevel=2)
    return tools


async def _list_page(session: Any, cursor: Any) -> Any:
    if cursor is None:
        return await session.list_tools()
    if "params" in inspect.signature(session.list_tools).parameters:  # MCP SDK 2.x
        from mcp.types import PaginatedRequestParams  # type: ignore[import-not-found,unused-ignore]

        return await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
    return await session.list_tools(cursor=cursor)


@asynccontextmanager
async def connect_stdio(
    command: str,
    args: Sequence[str] = (),
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    prefix: str | None = None,
    names: Iterable[str] | None = None,
    init_timeout: float = 30.0,
) -> AsyncIterator[list[Tool]]:
    """Start a stdio MCP server, yield its tools, and stop it when the block ends.

    ``async with connect_stdio("uvx", ["mcp-server-fetch"]) as tools: ...``

    A server that does not complete the MCP handshake within `init_timeout` seconds
    raises TimeoutError instead of hanging.
    """
    try:
        from mcp import ClientSession, StdioServerParameters  # type: ignore[import-not-found,unused-ignore]
        from mcp.client.stdio import stdio_client  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:
        raise ImportError(
            "connect_stdio needs the MCP SDK: pip install 'pi-python-core[mcp]'"
        ) from exc
    params = StdioServerParameters(command=command, args=list(args), env=env, cwd=cwd)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                async with asyncio.timeout(init_timeout):
                    await session.initialize()
                    tools = await mcp_tools(session, prefix=prefix, names=names)
                yield tools
    except BaseExceptionGroup as group:
        _raise_single(group)


@asynccontextmanager
async def connect_http(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    prefix: str | None = None,
    names: Iterable[str] | None = None,
    init_timeout: float = 30.0,
) -> AsyncIterator[list[Tool]]:
    """Connect to a streamable HTTP MCP server, yield its tools, and disconnect afterwards.

    ``async with connect_http("https://example.org/mcp", headers={...}) as tools: ...``

    The legacy SSE transport is not supported, as in Pi.
    """
    try:
        from mcp import ClientSession  # type: ignore[import-not-found,unused-ignore]
        from mcp.client import streamable_http as transport  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:
        raise ImportError(
            "connect_http needs the MCP SDK: pip install 'pi-python-core[mcp]'"
        ) from exc
    from mcp.shared._httpx_utils import create_mcp_http_client  # type: ignore[import-not-found,unused-ignore]

    origin = _origin(url)

    async def same_origin(request: Any) -> None:
        # Some SDK 1.x versions follow redirects to any origin, which would send the
        # headers (often an API key) there. Redirects within the server's origin, such
        # as /mcp to /mcp/, still work. SDK 2.x applies the same rule itself.
        target = _origin(str(request.url))
        if target != origin and target != ("https", *origin[1:]):
            raise ConnectionError(
                f"MCP server at {origin[1]} redirected to another origin ({target[1]});"
                " not following"
            )

    def client_without_redirects(*args: Any, **kwargs: Any) -> Any:
        client = create_mcp_http_client(*args, **kwargs)
        hooks = dict(client.event_hooks)
        hooks["request"] = [*hooks.get("request", []), same_origin]
        client.event_hooks = hooks
        return client

    try:
        async with AsyncExitStack() as stack:
            if hasattr(transport, "streamable_http_client"):  # SDK 2.x and late 1.x
                client = await stack.enter_async_context(
                    client_without_redirects(headers=dict(headers or {}))
                )
                streams = await stack.enter_async_context(
                    transport.streamable_http_client(url, http_client=client)
                )
            else:
                streams = await stack.enter_async_context(
                    transport.streamablehttp_client(  # type: ignore[attr-defined,unused-ignore]
                        url,
                        headers=dict(headers or {}),
                        httpx_client_factory=client_without_redirects,
                    )
                )
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            async with asyncio.timeout(init_timeout):
                await session.initialize()
                tools = await mcp_tools(session, prefix=prefix, names=names)
            yield tools
    except BaseExceptionGroup as group:
        _raise_single(group)


def _origin(url: str) -> tuple[str, str, int | None]:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    default = {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, parts.hostname or "", parts.port or default


def _raise_single(group: BaseExceptionGroup) -> None:
    # The SDK's task groups wrap a single failure ("Connection closed", a timeout, or an
    # error from the caller's block), sometimes twice; raise that failure itself.
    error: BaseException = group
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    if error is group:
        raise group
    raise error from None
