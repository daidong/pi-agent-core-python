import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from pi_python import *
from pi_python.mcp import connect_stdio, mcp_tools

SERVER = Path(__file__).parent / "servers" / "mcp_server.py"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


class FakeSession:
    """Duck-typed like the MCP SDK; `camel` mimics 1.x field names, `params` 2.x paging."""

    def __init__(self, tools, results, camel=False, params=False):
        self.pages = [tools[:1], tools[1:]]
        self.results = results
        self.camel = camel
        self.calls = []
        if params:
            self.list_tools = self._list_params

    def _page(self, cursor):
        index = 0 if cursor is None else int(cursor)
        nxt = str(index + 1) if index + 1 < len(self.pages) else None
        return NS(
            tools=self.pages[index], **({"nextCursor": nxt} if self.camel else {"next_cursor": nxt})
        )

    async def list_tools(self, cursor=None):
        return self._page(cursor)

    async def _list_params(self, *, params=None):
        return self._page(params.cursor if params else None)

    async def call_tool(self, name, arguments=None, progress_callback=None):
        self.calls.append((name, arguments))
        await progress_callback(1, 2, "half")
        return self.results[name]


def spec(name, schema=None, camel=False, **extra):
    key = "inputSchema" if camel else "input_schema"
    return NS(
        name=name, description=extra.get("description"), title=extra.get("title"), **{key: schema}
    )


@pytest.mark.parametrize("camel", [False, True], ids=["sdk2", "sdk1"])
async def test_content_conversion_paging_and_errors(camel):
    pytest.importorskip("mcp") if not camel else None  # 2.x paging builds PaginatedRequestParams
    err, sc = ("isError", "structuredContent") if camel else ("is_error", "structured_content")
    tools = [
        spec(
            "look up",
            {"type": "object", "properties": {"q": {"type": "string"}}},
            camel,
            description="Search",
        ),
        spec("stats", None, camel, title="Statistics"),
        spec("broken", {"$ref": "https://example.test/remote.json"}, camel),
    ]
    results = {
        "look up": NS(
            content=[
                NS(type="text", text="found"),
                NS(
                    type="image",
                    data=PNG,
                    **({"mimeType": "image/png"} if camel else {"mime_type": "image/png"}),
                ),
                NS(
                    type="audio",
                    data="",
                    **({"mimeType": "audio/wav"} if camel else {"mime_type": "audio/wav"}),
                ),
                NS(type="resource_link", name="doc", uri="file:///doc.txt"),
                NS(type="resource", resource=NS(uri="mem://a", text="inline")),
                NS(
                    type="resource",
                    resource=NS(
                        uri="mem://b",
                        blob="AAAA",
                        **(
                            {"mimeType": "application/zip"}
                            if camel
                            else {"mime_type": "application/zip"}
                        ),
                    ),
                ),
            ],
            **{err: False, sc: None},
        ),
        "stats": NS(content=[], **{err: False, sc: {"rows": 3}}),
    }
    session = FakeSession(tools, results, camel=camel, params=not camel)
    with pytest.warns(UserWarning, match="Skipping MCP tool broken"):
        wrapped = await mcp_tools(session, prefix="srv")
    assert [t.name for t in wrapped] == ["srv_look_up", "srv_stats"]
    assert wrapped[0].description == "Search" and wrapped[1].description == "Statistics"
    assert wrapped[1].input_schema == {"type": "object", "properties": {}}

    calls = [ToolCall("a", "srv_look_up", {"q": "x"}), ToolCall("b", "srv_stats", {})]
    p = ScriptedProvider([AssistantMessage(calls, "tool_use"), AssistantMessage.text("done")])
    agent = Agent(provider=p, tools=wrapped)
    updates = []
    agent.subscribe(
        lambda e: updates.append(e.data["update"]) if e.type == "tool_execution_update" else None
    )
    r = await agent.prompt("go")
    first, second = (o.result for o in r.tool_outcomes)
    assert [type(b).__name__ for b in first.content] == ["TextContent", "ImageContent"] + [
        "TextContent"
    ] * 4
    assert [b.text for b in first.content if isinstance(b, TextContent)] == [
        "found",
        "[audio audio/wav omitted]",
        "doc: file:///doc.txt",
        "inline",
        "[binary resource mem://b (application/zip) omitted]",
    ]
    assert second.structured_content == {"rows": 3} and '"rows": 3' in second.content[0].text
    assert session.calls == [("look up", {"q": "x"}), ("stats", {})]
    assert updates[0] == {"progress": 1, "total": 2, "message": "half"}


async def test_failures_and_input_requests_become_error_results():
    results = {
        "bad": NS(content=[], is_error=True, structured_content=None),
        "ask": NS(result_type="input_required"),
    }
    session = FakeSession([spec("bad"), spec("ask")], results)
    bad, ask = await mcp_tools(session, names=["bad", "ask"])
    context = ToolContext("run", "c", CancelToken(), lambda value: None)

    async def emit(value):
        pass

    context = ToolContext("run", "c", CancelToken(), emit)
    failed = (await run_tool_call(bad, ToolCall("c", "bad", {}), context)).result
    asked = (await run_tool_call(ask, ToolCall("c", "ask", {}), context)).result
    assert failed.is_error and failed.content[0].text == "MCP tool bad failed"
    assert asked.is_error and "asked for input" in asked.content[0].text


async def test_stdio_server_tools_run_through_the_agent():
    pytest.importorskip("mcp")
    async with connect_stdio(sys.executable, [str(SERVER)], prefix="demo") as tools:
        by_name = {t.name: t for t in tools}
        assert sorted(by_name) == ["demo_add", "demo_count", "demo_fail", "demo_pixel_png"]
        assert by_name["demo_add"].description == "Add two integers."
        calls = [
            ToolCall("c1", "demo_add", {"a": 2, "b": 3}),
            ToolCall("c2", "demo_fail", {"reason": "nope"}),
            ToolCall("c3", "demo_count", {"steps": 2}),
            ToolCall("c4", "demo_pixel_png", {}),
        ]
        p = ScriptedProvider([AssistantMessage(calls, "tool_use"), AssistantMessage.text("done")])
        agent = Agent(provider=p, tools=tools)
        updates = []
        agent.subscribe(
            lambda e: updates.append(e.data["update"])
            if e.type == "tool_execution_update"
            else None
        )
        r = await agent.prompt("go")
    add, fail, count, pixel = (o.result for o in r.tool_outcomes)
    assert r.status == "completed" and add.content[0].text == "5" and not add.is_error
    assert fail.is_error and "nope" in fail.content[0].text
    assert count.content[0].text == "counted 2" and [u["message"] for u in updates] == [
        "step 1",
        "step 2",
    ]
    assert isinstance(pixel.content[0], ImageContent) and pixel.content[0].mime_type == "image/png"


async def test_colliding_names_are_made_unique():
    session = FakeSession([spec("get.file"), spec("get_file")], {})
    with pytest.warns(UserWarning, match="renamed"):
        tools = await mcp_tools(session)
    assert tools[0].name == "get_file" and tools[1].name.startswith("get_file_")


async def test_unresponsive_or_crashing_servers_fail_clearly():
    pytest.importorskip("mcp")
    with pytest.raises(TimeoutError):
        async with connect_stdio(
            sys.executable, ["-c", "import time; time.sleep(30)"], init_timeout=0.5
        ):
            pass
    with pytest.raises(Exception) as caught:
        async with connect_stdio(sys.executable, ["-c", "pass"]):
            pass
    assert not isinstance(caught.value, BaseExceptionGroup)
