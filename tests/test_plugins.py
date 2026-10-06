"""Plugins: loading, combining hooks, skills, prompt templates, subagents, MCP servers."""

import asyncio
import importlib
import importlib.util
import inspect
import json
import socket
import subprocess
import sys
import textwrap
import time
import warnings
from pathlib import Path

import pytest

from pi_python import (
    AgentConfigUpdate,
    AssistantMessage,
    CancelToken,
    ConfigurationError,
    CustomMessage,
    Hooks,
    ModelEvent,
    ScriptedProvider,
    TextContent,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultUpdate,
    TurnUpdate,
    UserMessage,
    current_tools,
    tool,
)
from pi_python.plugins import (
    AgentDefinition,
    CheckResult,
    Plugin,
    PluginWarning,
    discover_plugins,
    load_plugins,
)
from pi_python.plugins._frontmatter import parse_frontmatter, parse_yaml

SERVER = Path(__file__).parent / "servers" / "mcp_server.py"
HAS_MCP = importlib.util.find_spec("mcp") is not None
# The MCP SDK's stdio server cannot start on PyPy (no fcntl.F_DUPFD_CLOEXEC).
STDIO_MCP = HAS_MCP and sys.implementation.name != "pypy"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def calls(*pairs, id_prefix="c"):
    return AssistantMessage(
        [ToolCall(f"{id_prefix}{i}", name, args) for i, (name, args) in enumerate(pairs)],
        "tool_use",
    )


def text_of(message):
    return "".join(b.text for b in message.content if isinstance(b, TextContent))


@tool
def echo(text: str) -> str:
    """Echo the text."""
    return text


# --- frontmatter ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "source, expected",
    [
        (
            "a: 1\nb: x y  # comment\nc: 'it''s'\nd: \"t\\u00e9\"",
            {"a": 1, "b": "x y", "c": "it's", "d": "té"},
        ),
        (
            "tools: [read, 'b c']\nmore:\n  - x\n  - y",
            {"tools": ["read", "b c"], "more": ["x", "y"]},
        ),
        ('allowed:\n  [\n    "Read",\n    "Write",\n  ]', {"allowed": ["Read", "Write"]}),
        ("d: >\n  one\n  two\n\n  three\n", {"d": "one two\nthree\n"}),
        ("d: |-\n  keep\n    indent\n", {"d": "keep\n  indent"}),
        ("d: plain\n  continued\nn: ~", {"d": "plain continued", "n": None}),
        ("d:\n  on its own line", {"d": "on its own line"}),
        ("d: Use when: the task says so", {"d": "Use when: the task says so"}),
        ("m:\n  k: v\n  list:\n  - 1\n  - 2", {"m": {"k": "v", "list": [1, 2]}}),
        ("items:\n  - name: a\n    on: true\n  - b", {"items": [{"name": "a", "on": True}, "b"]}),
        (
            '"quoted key": v\nurl: https://x.org/a#b',
            {"quoted key": "v", "url": "https://x.org/a#b"},
        ),
    ],
)
def test_frontmatter_subset(source, expected):
    assert parse_yaml(source) == expected


@pytest.mark.parametrize(
    "source",
    ["a: &anchor x", "a: {k: v}", "a:\n\tb: 1", "a: 1\na: 2", "a: 'open", "- x\nb: 1"],
)
def test_frontmatter_rejects_what_it_cannot_parse(source):
    with pytest.raises(ValueError):
        parse_yaml(source)


def test_frontmatter_without_header_is_body():
    assert parse_frontmatter("# Title\n") == ({}, "# Title\n")


# --- loading ----------------------------------------------------------------------------


def lab_plugin(root: Path) -> Path:
    write(
        root / "plugin.py",
        """
        from .ops import double

        __version__ = "1.2"


        def setup(api):
            api.add_tool(double)
            api.add_system_prompt("Lab rule: never delete raw data.")
        """,
    )
    write(
        root / "ops.py",
        '''
        from pi_python import tool


        @tool
        def double(x: int) -> int:
            """Double a number."""
            return 2 * x
        ''',
    )
    write(
        root / "skills" / "dedup" / "SKILL.md",
        """
        ---
        name: dedup
        description: Remove duplicate records. Use when a log has repeated events.
        ---
        Read references/semantics.md before choosing a window.
        """,
    )
    write(root / "skills" / "dedup" / "references" / "semantics.md", "Adjacent-gap grouping.\n")
    write(
        root / "prompts" / "dedup.md",
        """
        ---
        description: Deduplicate a file
        argument-hint: <file> [window]
        ---
        Deduplicate $1 with a ${2:-60} second window.
        """,
    )
    write(
        root / "agents" / "checker.md",
        """
        ---
        name: checker
        description: Checks results
        tools: double
        ---
        You check results.
        """,
    )
    return root


async def test_directory_plugin_conventions_and_code(tmp_path):
    root = lab_plugin(tmp_path / "lab")
    async with load_plugins([str(root)]) as plugins:
        (plugin,) = plugins.plugins
        assert (plugin.name, plugin.version, plugin.root) == ("lab", "1.2", root.resolve())
        assert [t.name for t in plugins.tools] == ["double"]
        assert [s.name for s in plugins.skills] == ["dedup"]
        assert [(t.name, t.argument_hint) for t in plugins.prompts] == [
            ("dedup", "<file> [window]")
        ]
        assert [(a.name, a.tools, a.plugin) for a in plugins.agents] == [
            ("checker", ["double"], "lab")
        ]
        prompt = plugins.system_prompt("You are a lab assistant.")
        assert prompt.startswith("You are a lab assistant.\n\nLab rule: never delete raw data.\n\n")
        assert "<name>dedup</name>" in prompt and "read_skill" in prompt
        assert (
            plugins.expand("/dedup events.csv") == "Deduplicate events.csv with a 60 second window."
        )
        assert plugins.expand("plain text") == "plain text"
        expanded = plugins.expand("/skill:dedup look at events.csv")
        assert expanded.startswith('<skill name="dedup" location="')
        assert expanded.endswith("</skill>\n\nlook at events.csv")


async def test_resource_only_directory_and_single_file(tmp_path):
    write(tmp_path / "docs" / "prompts" / "hello.md", "Say hello to $1\n")
    write(
        tmp_path / "extra.py",
        """
        def setup(api):
            api.add_system_prompt(f"from {api.name}")
        """,
    )
    async with load_plugins([tmp_path / "docs", str(tmp_path / "extra.py")]) as plugins:
        assert [p.name for p in plugins.plugins] == ["docs", "extra"]
        assert plugins.plugins[1].root is None
        assert plugins.expand("/hello Ada") == "Say hello to Ada\n"
        assert plugins.system_prompt() == "from extra"


async def test_inline_plugin_options_and_services():
    seen = {}

    async def setup(api):
        seen["options"] = api.options
        seen["db"] = api.service("db")
        seen["fallback"] = api.service("chooser", None)

    async with load_plugins(
        [Plugin("inline", setup)], services={"db": "conn"}, options={"inline": {"window": 60}}
    ):
        pass
    assert seen == {"options": {"window": 60}, "db": "conn", "fallback": None}


async def test_missing_service_names_the_fix():
    with pytest.raises(ConfigurationError, match=r"services=\{'chooser': \.\.\.\}"):
        async with load_plugins(Plugin("p", lambda api: api.service("chooser"))):
            pass


@pytest.mark.parametrize(
    "sources, options, message",
    [
        ([Plugin("a"), Plugin("a")], None, "Two plugins are named 'a'"),
        ([Plugin("a")], {"b": {}}, "not loaded: \\['b'\\]"),
        (["no-such-plugin-xyz"], None, "No installed plugin is named 'no-such-plugin-xyz'"),
        (["./does/not/exist"], None, "does not exist"),
    ],
)
async def test_load_errors(sources, options, message):
    with pytest.raises(ConfigurationError, match=message):
        async with load_plugins(sources, options=options):
            pass


async def test_setup_failure_closes_plugins_already_set_up():
    closed = []

    def good(api):
        api.on_close(lambda: closed.append("good"))

    def bad(api):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom") as info:
        async with load_plugins([Plugin("good", good), Plugin("bad", bad)]):
            pass
    assert closed == ["good"]
    assert "while setting up plugin 'bad'" in "".join(info.value.__notes__)


async def test_tool_names_must_be_unique():
    def provides_echo(api):
        api.add_tool(echo)

    with pytest.raises(ConfigurationError, match="both provide the tool 'echo'"):
        async with load_plugins([Plugin("a", provides_echo), Plugin("b", provides_echo)]):
            pass
    async with load_plugins(Plugin("a", provides_echo)) as plugins:
        with pytest.raises(ConfigurationError, match="Two tools are named 'echo'"):
            plugins.agent(provider=ScriptedProvider([]), tools=[echo])


async def test_api_misuse_is_reported():
    with pytest.raises(ConfigurationError, match="Unknown hook 'before_tool'"):
        async with load_plugins(Plugin("p", lambda api: api.on("before_tool", print))):
            pass
    with pytest.raises(ConfigurationError, match="no root directory"):
        async with load_plugins(Plugin("p", lambda api: api.add_skills("skills"))):
            pass
    with pytest.raises(ConfigurationError, match="Open the PluginSet first"):
        load_plugins([]).agent()


@pytest.fixture
def fresh_demo_package():
    """Each test installs `demo_plugin` in its own directory; forget earlier imports."""

    def forget():
        for name in [m for m in sys.modules if m.split(".")[0] == "demo_plugin"]:
            del sys.modules[name]

    forget()
    yield
    forget()


def make_distribution(site: Path):
    write(
        site / "demo_plugin" / "__init__.py",
        """
        from pi_python.plugins import Plugin


        def setup(api):
            api.add_system_prompt("module setup")


        def setup_fn(api):
            api.add_system_prompt("function setup")


        PLUGIN = Plugin("ignored-name", lambda api: api.add_system_prompt("object setup"))
        """,
    )
    write(
        site / "demo_plugin" / "skills" / "demo" / "SKILL.md",
        "---\nname: demo\ndescription: A demo skill.\n---\nDemo.\n",
    )
    dist = site / "demo_plugin-0.3.dist-info"
    write(dist / "METADATA", "Metadata-Version: 2.1\nName: demo-plugin\nVersion: 0.3\n")
    write(
        dist / "entry_points.txt",
        """
        [pi_python.plugins]
        demo = demo_plugin
        demo-fn = demo_plugin:setup_fn
        demo-obj = demo_plugin:PLUGIN
        """,
    )


async def test_entry_point_plugins(tmp_path, monkeypatch, fresh_demo_package):
    make_distribution(tmp_path / "site")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    importlib.invalidate_caches()
    found = {p.name: p for p in discover_plugins()}
    assert found["demo"].target == "demo_plugin"
    assert (found["demo"].distribution, found["demo"].version) == ("demo-plugin", "0.3")
    async with load_plugins(["demo", "demo-fn", "demo-obj"]) as plugins:
        assert [p.name for p in plugins.plugins] == ["demo", "demo-fn", "demo-obj"]
        assert {p.version for p in plugins.plugins} == {"0.3"}
        # Every form gets the package directory as its root, so the skill loads once.
        assert [s.name for s in plugins.skills] == ["demo"]
        assert plugins.system_prompt().split("\n\n")[:3] == [
            "module setup",
            "function setup",
            "object setup",
        ]
    assert plugins.diagnostics == []


# --- combining hooks --------------------------------------------------------------------


async def test_before_tool_call_first_block_wins():
    order = []

    def app(call, args, context):
        order.append("app")

    def blocker(api):
        api.on("before_tool_call", lambda call, args, ctx: order.append("a") or False)

    def later(api):
        api.on("before_tool_call", lambda call, args, ctx: order.append("b"))

    provider = ScriptedProvider([calls(("echo", {"text": "hi"})), AssistantMessage.text("done")])
    async with load_plugins([Plugin("a", blocker), Plugin("b", later)]) as plugins:
        agent = plugins.agent(provider=provider, tools=[echo], hooks=Hooks(before_tool_call=app))
        result = await agent.prompt("go")
    assert order == ["app", "a"]
    assert result.tool_outcomes[0].result.error_code == "blocked"


async def test_before_tool_call_plugin_exception_fails_only_that_call():
    def raising(api):
        api.on("before_tool_call", lambda call, args, ctx: 1 / 0)

    provider = ScriptedProvider([calls(("echo", {"text": "hi"})), AssistantMessage.text("ok")])
    async with load_plugins(Plugin("p", raising)) as plugins:
        result = await plugins.agent(provider=provider, tools=[echo]).prompt("go")
    assert result.status == "completed"
    assert result.tool_outcomes[0].result.error_code == "hook_error"


async def test_after_tool_call_chains_and_isolates_failures():
    errors = []

    def first(api):
        api.on("after_tool_call", lambda c, r, ctx: ToolResultUpdate(content=[TextContent("A")]))

    def broken(api):
        api.on("after_tool_call", lambda c, r, ctx: 1 / 0)

    def last(api):
        @api.on("after_tool_call")
        def record(call, result, context):
            assert context.result.content[0].text == "A"
            return ToolResultUpdate(details={"seen": result.content[0].text})

    provider = ScriptedProvider([calls(("echo", {"text": "hi"})), AssistantMessage.text("ok")])
    sources = [Plugin("first", first), Plugin("broken", broken), Plugin("last", last)]
    async with load_plugins(sources, on_error=errors.append) as plugins:
        result = await plugins.agent(provider=provider, tools=[echo]).prompt("go")
    final = result.tool_outcomes[0].result
    assert (final.content[0].text, final.details) == ("A", {"seen": "A"})
    assert [(e.plugin, e.hook, type(e.error)) for e in errors] == [
        ("broken", "after_tool_call", ZeroDivisionError)
    ]
    assert "plugin 'broken' failed in after_tool_call" in str(errors[0])


async def test_application_hook_errors_keep_core_behavior():
    def plugin(api):
        api.on("after_tool_call", lambda c, r, ctx: None)

    def app(call, result, context):
        raise ValueError("app bug")

    provider = ScriptedProvider([calls(("echo", {"text": "hi"})), AssistantMessage.text("ok")])
    async with load_plugins(Plugin("p", plugin)) as plugins:
        agent = plugins.agent(provider=provider, tools=[echo], hooks=Hooks(after_tool_call=app))
        result = await agent.prompt("go")
    assert result.tool_outcomes[0].result.error_code == "finalization_error"


async def test_context_hooks_chain():
    def notes(api):
        api.on(
            "transform_context",
            lambda messages, cancel: [*messages, CustomMessage("note", "from notes")],
        )
        api.on(
            "convert_to_llm",
            lambda messages: [
                UserMessage(f"[note] {m.data}")
                if isinstance(m, CustomMessage) and m.custom_type == "note"
                else m
                for m in messages
            ],
        )

    def tags(api):
        api.on(
            "transform_context",
            lambda messages, cancel: [*messages, CustomMessage("tag", "from tags")],
        )
        api.on(
            "convert_to_llm",
            lambda messages: [
                UserMessage(f"[tag] {m.data}") if isinstance(m, CustomMessage) else m
                for m in messages
            ],
        )

    provider = ScriptedProvider([AssistantMessage.text("ok")])
    async with load_plugins([Plugin("notes", notes), Plugin("tags", tags)]) as plugins:
        result = await plugins.agent(provider=provider).prompt("hi")
    assert result.status == "completed"
    sent = [m.content for m in provider.requests[0].messages if isinstance(m, UserMessage)]
    assert sent == ["hi", "[note] from notes", "[tag] from tags"]


async def test_prepare_request_handlers_see_earlier_updates():
    def first(api):
        api.on(
            "prepare_request",
            lambda ctx, cancel: TurnUpdate(
                options={**ctx.options, "temperature": 0.1}, messages=[UserMessage("from first")]
            ),
        )

    def second(api):
        @api.on("prepare_request")
        def prepare(context, cancel):
            assert context.options["temperature"] == 0.1
            # Messages are appended after every handler ran.
            assert "from first" not in [getattr(m, "content", None) for m in context.messages]
            return TurnUpdate(messages=[UserMessage("from second")])

    provider = ScriptedProvider([AssistantMessage.text("ok")])
    errors = []
    sources = [Plugin("first", first), Plugin("second", second)]
    async with load_plugins(sources, on_error=errors.append) as plugins:
        await plugins.agent(provider=provider).prompt("hi")
    assert errors == []
    request = provider.requests[0]
    assert request.options["temperature"] == 0.1
    assert [m.content for m in request.messages if isinstance(m, UserMessage)] == [
        "hi",
        "from first",
        "from second",
    ]


async def test_finish_turn_end_wins_and_bad_values_are_reported():
    errors = []

    def keep_going(api):
        api.on("finish_turn", lambda ctx, cancel: "continue")

    def stop(api):
        api.on("finish_turn", lambda ctx, cancel: "end")

    def wrong(api):
        api.on("finish_turn", lambda ctx, cancel: "stop please")

    provider = ScriptedProvider([calls(("echo", {"text": "hi"})), AssistantMessage.text("unused")])
    sources = [Plugin("go", keep_going), Plugin("stop", stop), Plugin("wrong", wrong)]
    async with load_plugins(sources, on_error=errors.append) as plugins:
        result = await plugins.agent(provider=provider, tools=[echo]).prompt("go")
    assert (result.status, result.stop_reason) == ("completed", "finish_turn")
    assert len(provider.requests) == 1
    assert [e.plugin for e in errors] == ["wrong"]


async def test_provider_callbacks_compose():
    seen = []

    def plugin_a(api):
        api.on("get_api_key", lambda provider: None)
        api.on("on_payload", lambda payload: {**payload, "a": 1})
        api.on("on_response", lambda response: seen.append(("a", response)))

    def plugin_b(api):
        api.on("get_api_key", lambda provider: f"key-for-{provider}")
        api.on("on_payload", lambda payload: payload.update(b=2))  # in place, returns None
        api.on("on_response", lambda response: 1 / 0)

    errors = []
    sources = [Plugin("a", plugin_a), Plugin("b", plugin_b)]
    async with load_plugins(sources, on_error=errors.append) as plugins:
        hooks = plugins.hooks(Hooks(on_response=lambda response: seen.append(("app", response))))
        assert await hooks.get_api_key("anthropic") == "key-for-anthropic"
        assert await hooks.on_payload({"x": 0}) == {"x": 0, "a": 1, "b": 2}
        await hooks.on_response({"status": 200})
        untouched = plugins.hooks(Hooks(finish_turn=print))
    assert seen == [("app", {"status": 200}), ("a", {"status": 200})]
    assert [e.plugin for e in errors] == ["b"]
    assert untouched.finish_turn is print  # no plugin handler: the application's hook as is


async def test_agent_keyword_callbacks_join_the_composition():
    order = []

    def plugin(api):
        api.on("on_payload", lambda payload: order.append("plugin"))

    async with load_plugins(Plugin("p", plugin)) as plugins:
        agent = plugins.agent(
            provider=ScriptedProvider([]), on_payload=lambda payload: order.append("app")
        )
        await agent.hooks.on_payload({})
    assert order == ["app", "plugin"]


async def test_event_listeners_are_isolated():
    events, errors = [], []

    def watcher(api):
        api.subscribe(lambda event: events.append(event.type))

    def broken(api):
        api.subscribe(lambda event: 1 / 0)

    provider = ScriptedProvider([AssistantMessage.text("ok")])
    sources = [Plugin("broken", broken), Plugin("watcher", watcher)]
    async with load_plugins(sources, on_error=errors.append) as plugins:
        result = await plugins.agent(provider=provider).prompt("hi")
    assert result.status == "completed"
    assert events[0] == "agent_start" and events[-1] == "agent_end"
    assert {e.plugin for e in errors} == {"broken"}


# --- skills -----------------------------------------------------------------------------


async def test_read_skill_tool(tmp_path):
    root = lab_plugin(tmp_path / "lab")
    write(
        root / "skills" / "manual" / "SKILL.md",
        "---\nname: manual\ndescription: Only by command.\ndisable-model-invocation: true\n---\nx\n",
    )
    write(tmp_path / "secret.txt", "outside")
    async with load_plugins(root) as plugins:
        reader = plugins.skill_tool()
        assert reader.input_schema["properties"]["name"]["enum"] == ["dedup"]
        assert "manual" not in plugins.system_prompt()

        async def read(**args):
            return await reader.execute(args, None)

        block = (await read(name="dedup")).content[0].text
        assert block.startswith('<skill name="dedup"') and "Read references/semantics.md" in block
        reference = await read(name="dedup", path="references/semantics.md")
        assert reference.content[0].text == "Adjacent-gap grouping.\n"
        listing = await read(name="dedup", path=".")
        assert listing.content[0].text == "SKILL.md\nreferences/"
        escape = await read(name="dedup", path="../../../secret.txt")
        assert escape.is_error and "outside the skill directory" in escape.content[0].text
        missing = await read(name="dedup", path="nope.md")
        assert missing.is_error
        # The manual skill still expands by explicit command.
        assert plugins.expand("/skill:manual").startswith('<skill name="manual"')


async def test_skill_name_collisions_keep_the_first(tmp_path):
    for name in ("one", "two"):
        write(
            tmp_path / name / "skills" / "shared" / "SKILL.md",
            f"---\nname: shared\ndescription: from {name}\n---\n",
        )
    with pytest.warns(PluginWarning, match="hidden by the one of plugin 'one'"):
        async with load_plugins([tmp_path / "one", tmp_path / "two"]) as plugins:
            assert [s.description for s in plugins.skills] == ["from one"]


async def test_invalid_skill_is_reported_not_fatal(tmp_path):
    write(tmp_path / "p" / "skills" / "bad" / "SKILL.md", "---\nname: bad\n---\n")
    with pytest.warns(PluginWarning, match="description is required"):
        async with load_plugins(tmp_path / "p") as plugins:
            assert plugins.skills == []
    assert any("description is required" in d for d in plugins.diagnostics)


# --- subagents --------------------------------------------------------------------------


def delegating(*steps):
    return AssistantMessage([ToolCall("s1", "subagent", dict(steps))], "tool_use")


async def test_subagent_from_markdown_shares_the_main_provider(tmp_path):
    root = lab_plugin(tmp_path / "lab")
    provider = ScriptedProvider(
        [
            delegating(("agent", "checker"), ("task", "Is 2*21 = 42?")),
            calls(("double", {"x": 21})),
            AssistantMessage.text("Yes, 42.", usage={"input": 5, "output": 2}),
            AssistantMessage.text("The checker confirmed 42."),
        ]
    )
    async with load_plugins(root) as plugins:
        agent = plugins.agent(provider=provider, model="mock", tools=[echo])
        declared = {t.name for t in current_tools(list(agent.state.messages))}
        assert declared == {"echo", "double", "read_skill", "subagent"}
        result = await agent.prompt("check it")
    outcome = result.tool_outcomes[0].result
    assert outcome.content[0].text == "Yes, 42."
    assert outcome.usage == {"input": 5, "output": 2}
    assert outcome.details["mode"] == "single"
    inner = provider.requests[1]
    assert [t.name for t in inner.tools] == ["double"]  # only the tools it asked for
    assert inner.messages[0].content.startswith("You check results.")
    assert result.messages[-1].content[0].text == "The checker confirmed 42."


async def test_subagent_chain_parallel_and_failures():
    def answers(*texts):
        return ScriptedProvider([AssistantMessage.text(t) for t in texts])

    scout = answers("found a.py")
    planner = answers("plan for a.py")
    worker = answers("w1", "w2")
    broken = ScriptedProvider([RuntimeError("model down")])

    def setup(api):
        for name, provider in (
            ("scout", scout),
            ("planner", planner),
            ("worker", worker),
            ("broken", broken),
        ):
            api.add_agent(AgentDefinition(name, f"The {name}", provider=provider, tools=[]))

    updates = []

    async def emit(value):
        updates.append(value)

    async with load_plugins(Plugin("team", setup)) as plugins:
        run = plugins.subagent_tool().execute
        context = ToolContext("run", "call", CancelToken(), emit)
        chain = await run(
            {
                "chain": [
                    {"agent": "scout", "task": "find"},
                    {"agent": "planner", "task": "{previous}!"},
                ]
            },
            context,
        )
        assert chain.content[0].text == "plan for a.py"
        assert planner.requests[0].messages[-1].content == "found a.py!"

        parallel = await run(
            {"tasks": [{"agent": "worker", "task": "one"}, {"agent": "worker", "task": "two"}]},
            context,
        )
        assert parallel.content[0].text.startswith("Parallel: 2/2 succeeded")
        assert not parallel.is_error

        failed = await run(
            {"chain": [{"agent": "broken", "task": "x"}, {"agent": "scout", "task": "y"}]}, context
        )
        assert failed.is_error
        assert failed.content[0].text.startswith("Chain stopped at step 1 (broken): Agent failed")

        invalid = await run({"agent": "scout"}, context)
        assert invalid.is_error and "Provide exactly one mode" in invalid.content[0].text
        too_many = await run({"tasks": [{"agent": "scout", "task": "t"}] * 9}, context)
        assert too_many.is_error
    assert {"agent": "scout", "status": "completed"} in updates


async def test_subagent_unknown_tools_warn():
    def setup(api):
        api.add_agent(AgentDefinition("x", "X", tools=["read", "bash"]))

    async with load_plugins(Plugin("p", setup)) as plugins:
        with pytest.warns(PluginWarning, match="not available: read, bash"):
            plugins.agent(provider=ScriptedProvider([]))


class WaitsForCancel:
    """A provider whose answer never comes; it ends only when cancelled."""

    def __init__(self):
        self.started = asyncio.Event()

    async def stream(self, request, cancel):
        self.started.set()
        await cancel.wait()
        cancel.raise_if_cancelled()
        yield ModelEvent.done(AssistantMessage.text("never"))


async def test_cancelling_the_main_agent_aborts_subagents():
    slow = WaitsForCancel()

    def setup(api):
        api.add_agent(AgentDefinition("slow", "Slow", provider=slow))

    provider = ScriptedProvider([delegating(("agent", "slow"), ("task", "wait"))])
    async with load_plugins(Plugin("p", setup)) as plugins:
        agent = plugins.agent(provider=provider)
        running = asyncio.create_task(agent.prompt("go"))
        await asyncio.wait_for(slow.started.wait(), 5)
        agent.abort("user stop")
        result = await asyncio.wait_for(running, 5)
    assert result.status == "cancelled"
    outcome = result.tool_outcomes[0].result
    assert outcome.is_error


# --- checks and closing ----------------------------------------------------------------


async def test_checks_and_close_order():
    closed, errors = [], []

    def setup(api):
        api.add_check(lambda: None, "passes")
        api.add_check(lambda: False, "returns_false")

        def raises():
            raise AssertionError("window must be positive")

        api.add_check(raises)
        api.on_close(lambda: closed.append(1))
        api.on_close(lambda: 1 / 0)

        async def last():
            closed.append(2)

        api.on_close(last)

    async with load_plugins(Plugin("p", setup), on_error=errors.append) as plugins:
        results = await plugins.check()
    assert results == [
        CheckResult("p", "passes", True),
        CheckResult("p", "returns_false", False, "returned False"),
        CheckResult("p", "raises", False, "AssertionError: window must be positive"),
    ]
    assert closed == [2, 1]
    assert [e.hook for e in errors] == ["on_close"]


# --- MCP servers ------------------------------------------------------------------------


def mcp_plugin(root: Path, servers: dict) -> Path:
    import json

    write(root / "mcp.json", json.dumps({"mcpServers": servers}))
    return root


@pytest.mark.skipif(not STDIO_MCP, reason="needs the [mcp] extra on CPython")
async def test_plugin_mcp_servers(tmp_path):
    root = mcp_plugin(
        tmp_path / "calc",
        {
            "calc": {"command": "${PYTHON}", "args": [str(SERVER)], "timeout": 5},
            "off": {"command": "nothing", "enabled": False},
            "missing": {"command": str(tmp_path / "no-such-binary")},
        },
    )
    provider = ScriptedProvider(
        [calls(("mcp__calc__add", {"a": 19, "b": 23})), AssistantMessage.text("42")]
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with load_plugins(root) as plugins:
            names = [t.name for t in plugins.tools]
            assert "mcp__calc__add" in names and not any("off" in n for n in names)
            result = await plugins.agent(provider=provider).prompt("add")
    assert result.tool_outcomes[0].result.content[0].text == "42"
    messages = [str(w.message) for w in caught if w.category is PluginWarning]
    assert any("ignoring unsupported settings timeout" in m for m in messages)
    assert any("MCP server 'missing' did not connect" in m for m in messages)


@pytest.mark.skipif(not STDIO_MCP, reason="needs the [mcp] extra on CPython")
def test_plugin_mcp_servers_in_a_plain_script(tmp_path):
    # Opening and closing happen in two blocking calls; the servers must still close cleanly.
    root = mcp_plugin(tmp_path / "calc", {"calc": {"command": "${PYTHON}", "args": [str(SERVER)]}})
    provider = ScriptedProvider(
        [calls(("mcp__calc__add", {"a": 1, "b": 2})), AssistantMessage.text("3")]
    )
    with load_plugins(root) as plugins:
        result = plugins.agent(provider=provider).prompt_sync("add")
    assert result.tool_outcomes[0].result.content[0].text == "3"


async def test_mcp_configuration_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("PI_PLUGIN_TEST_TOKEN", raising=False)

    def server(name, config):
        return Plugin("p", lambda api: api.add_mcp_server(name, config))

    for name, config, message in [
        ("s", {"url": "http://x", "type": "sse"}, "SSE transport is not supported"),
        ("s", {"description": "nothing to run"}, "needs a command or a url"),
        ("s", {"url": "http://x", "headers": {"A": "${PI_PLUGIN_TEST_TOKEN}"}}, "is not set"),
        ("s", {"command": "x", "args": "--flag"}, "args must be a list"),
        ("s", {"command": "x", "process_scope": "yes"}, "process_scope must be a boolean"),
        ("s", {"url": "http://x", "process_scope": True}, "process_scope must be a boolean"),
        ("bad name", {"command": "x"}, "may only use letters"),
        ("s", {"command": "${PLUGIN_ROOT}/x"}, "needs a plugin directory"),
    ]:
        with pytest.raises(ConfigurationError, match=message):
            async with load_plugins(server(name, config)):
                pass

    def two(api, name):
        api.add_mcp_server(name, {"command": "x", "enabled": False})

    with pytest.raises(ConfigurationError, match="conflicts with a server of plugin 'a'"):
        async with load_plugins(
            [Plugin("a", lambda api: two(api, "my-db")), Plugin("b", lambda api: two(api, "my_db"))]
        ):
            pass


async def test_mcp_variables_expand(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_PLUGIN_TEST_TOKEN", "secret")
    write(tmp_path / "p" / "plugin.py", "def setup(api):\n    pass\n")
    mcp_plugin(
        tmp_path / "p",
        {
            "local": {
                "command": "${PYTHON}",
                "args": ["${PLUGIN_ROOT}/server.py"],
                "env": {"TOKEN": "${PI_PLUGIN_TEST_TOKEN}"},
                "enabled": False,
            },
            "remote": {
                "url": "https://x.org/mcp",
                "headers": {"Authorization": "Bearer ${PI_PLUGIN_TEST_TOKEN}"},
                "call_metadata": {"context_id": "${LITERAL}", "nested": [None, True, 3]},
                "enabled": False,
            },
        },
    )
    async with load_plugins(tmp_path / "p") as plugins:
        servers = plugins._apis[0]._mcp
    assert servers["local"]["command"] == sys.executable
    assert servers["local"]["args"] == [f"{(tmp_path / 'p').resolve()}/server.py"]
    assert servers["local"]["env"] == {"TOKEN": "secret"}
    assert servers["remote"]["headers"] == {"Authorization": "Bearer secret"}
    assert servers["remote"]["call_metadata"] == {
        "context_id": "${LITERAL}",
        "nested": [None, True, 3],
    }


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not HAS_MCP, reason="needs the [mcp] extra")
async def test_connect_http():
    from pi_python.mcp import connect_http

    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(SERVER), "--http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline or process.poll() is not None:
                    pytest.fail("the HTTP MCP server did not start")
                await asyncio.sleep(0.1)
        async with connect_http(f"http://127.0.0.1:{port}/mcp", prefix="web") as tools:
            add = next(t for t in tools if t.name == "web_add")

            class Context:
                async def emit_update(self, value):
                    pass

            result = await add.execute({"a": 2, "b": 3}, Context())
            assert result.content[0].text == "5"
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(params=["stdio", "http"])
async def metadata_server(request):
    if not HAS_MCP or (request.param == "stdio" and not STDIO_MCP):
        pytest.skip("needs the [mcp] extra and a supported server runtime")
    if request.param == "stdio":
        yield {"command": sys.executable, "args": [str(SERVER)]}
        return
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(SERVER), "--http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline or process.poll() is not None:
                    pytest.fail("the HTTP MCP server did not start")
                await asyncio.sleep(0.1)
        yield {"url": f"http://127.0.0.1:{port}/mcp"}
    finally:
        process.terminate()
        process.wait(timeout=10)


async def test_plugin_metadata_roundtrip_and_concurrent_contexts(metadata_server):
    from mcp import ClientSession

    if "meta" not in inspect.signature(ClientSession.call_tool).parameters:
        pytest.skip("SDK does not support call metadata; tested separately")

    def setup(api):
        metadata = api.options["metadata"]
        api.add_mcp_server(api.name, {**metadata_server, "call_metadata": metadata})
        metadata["nested"]["ids"].append("changed after registration")

    async def run_task(task):
        options = {
            name: {"metadata": {"context_id": f"{task}-{name}", "nested": {"ids": [task]}}}
            for name in ("one", "two")
        }
        async with load_plugins(
            [Plugin("one", setup), Plugin("two", setup)],
            options=options,
        ) as plugins:
            status = await plugins.readiness(required_mcp_servers=["one", "two"])
            assert status.ready
            tools = [t for t in plugins.tools if t.name.endswith("__metadata")]
            assert len(tools) == 2
            for t in tools:
                assert set(t.input_schema["properties"]) == {"value"}
            provider = ScriptedProvider(
                [
                    calls(*[(t.name, {"value": "model argument"}) for t in tools]),
                    AssistantMessage.text("done"),
                ]
            )
            agent = plugins.agent(provider=provider)
            updates = []
            agent.subscribe(
                lambda e: updates.append(e.data["update"])
                if e.type == "tool_execution_update"
                else None
            )
            result = await agent.prompt("go")
            assert len(updates) == 2
            for outcome, name in zip(result.tool_outcomes, ("one", "two"), strict=True):
                assert not outcome.result.is_error
                received = json.loads(outcome.result.content[0].text)
                assert received["arguments"] == {"value": "model argument"}
                meta = received["meta"]
                assert meta["context_id"] == f"{task}-{name}"
                assert meta["nested"] == {"ids": [task]}
                assert "progressToken" in meta or "progress_token" in meta
            for request in provider.requests:
                assert "context_id" not in repr(request.tools)

    await asyncio.gather(run_task("task-a"), run_task("task-b"))


async def test_unsupported_sdk_metadata_fails_plugin_loading(metadata_server):
    from mcp import ClientSession

    if "meta" in inspect.signature(ClientSession.call_tool).parameters:
        pytest.skip("requires an old SDK without meta")
    closed = []

    def setup(api):
        api.on_close(lambda: closed.append(True))
        api.add_mcp_server("sandbox", {**metadata_server, "call_metadata": {}})

    with pytest.raises(ConfigurationError, match="call_metadata.*meta"):
        async with load_plugins(Plugin("p", setup)):
            pytest.fail("unsupported metadata must not silently load")
    assert closed == [True]


@pytest.mark.parametrize("strict", [False, True])
async def test_strict_loading_rejects_diagnostics_and_closes_resources(strict, monkeypatch):
    from contextlib import asynccontextmanager
    import pi_python.mcp

    events = []

    @asynccontextmanager
    async def connect(command, args, **kwargs):
        if command == "broken":
            raise ConnectionError("unavailable")
        events.append("connected")
        try:
            yield []
        finally:
            events.append("disconnected")

    monkeypatch.setattr(pi_python.mcp, "connect_stdio", connect)

    def setup(api):
        api.on_close(lambda: events.append("closed"))
        api.add_mcp_server("good", {"command": "good"})
        api.add_mcp_server("bad", {"command": "broken"})
        api.add_mcp_server("disabled", {"command": "good", "enabled": False})

    plugins = load_plugins(Plugin("p", setup), strict=strict)
    if strict:
        with pytest.raises(ConfigurationError, match="Strict plugin loading.*unavailable"):
            await plugins.open()
    else:
        with pytest.warns(PluginWarning, match="unavailable"):
            await plugins.open()
        report = await plugins.readiness(required_mcp_servers=["good", "bad", "disabled"])
        assert report.loaded and not report.ready
        assert report.missing_mcp_servers == ("bad", "disabled")
        with pytest.raises(ConfigurationError, match="missing MCP servers"):
            report.require_ready()
        await plugins.aclose()
    assert events == ["connected", "disconnected", "closed"]
    report = await plugins.readiness(required_mcp_servers=["good"])
    assert not report.loaded and not report.ready and report.missing_mcp_servers == ("good",)
    await plugins.aclose()
    assert events.count("closed") == 1


async def test_strict_loading_rejects_invalid_resources(tmp_path):
    write(tmp_path / "skills" / "invalid" / "SKILL.md", "No description")
    write(tmp_path / "mcp.json", '{"mcpServers": {"broken": {"type": "sse"}}}')
    with pytest.raises(ConfigurationError, match="Strict plugin loading") as caught:
        async with load_plugins(tmp_path, strict=True):
            pass
    assert "description is required" in str(caught.value)
    assert "SSE transport" in str(caught.value)


async def test_readiness_requires_named_checks_and_resources(tmp_path):
    write(
        tmp_path / "skills" / "rules" / "SKILL.md",
        "---\nname: rules\ndescription: Rules\n---\nRules",
    )
    ran = []

    @tool
    def validate() -> str:
        """Validate rules."""
        return "ok"

    def setup(api):
        api.add_tool(validate)
        api.add_check(lambda: ran.append("p"), "health")

    async with load_plugins(Plugin("p", setup, root=tmp_path), strict=True) as plugins:
        report = await plugins.readiness(
            required_tools=["validate", "read_skill"],
            required_skills=["rules"],
            required_checks=[("p", "health")],
        )
        assert report.ready and report.plugins == ("p",) and ran == ["p"]
        report.require_ready()
        report = await plugins.readiness(
            required_tools=["sandbox"],
            required_skills=["absent"],
            required_checks=[("other-plugin", "health")],
        )
        assert not report.ready
        assert report.missing_tools == ("sandbox",) and report.missing_skills == ("absent",)
        assert report.missing_checks == (("other-plugin", "health"),)


async def test_readiness_empty_and_failed_checks_are_distinct():
    plugins = load_plugins(Plugin("empty"), strict=True)
    assert not (await plugins.readiness()).ready
    async with plugins:
        report = await plugins.readiness(required_checks=[("empty", "health")])
        assert report.loaded and not report.ready and report.checks == ()
        assert report.missing_checks == (("empty", "health"),)

    def setup(api):
        api.add_check(lambda: False, "false")
        api.add_check(lambda: 1 / 0, "error")

    async with load_plugins(Plugin("failed", setup), strict=True) as plugins:
        report = await plugins.readiness(required_checks=[("failed", "false"), ("failed", "error")])
        assert report.loaded and not report.ready and not report.missing_checks
        assert len(report.checks) == 2 and not any(c.passed for c in report.checks)
        with pytest.raises(ConfigurationError, match="failed/false.*failed/error"):
            report.require_ready()


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
async def test_readiness_rejects_invalid_probe_timeout(timeout):
    async with load_plugins(Plugin("empty")) as plugins:
        with pytest.raises(ConfigurationError, match="mcp_timeout"):
            await plugins.readiness(mcp_timeout=timeout)


async def test_readiness_probes_are_bounded_cancelled_and_can_recover(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import pi_python.mcp

    healthy = False
    entered, stopped = set(), set()

    @asynccontextmanager
    async def connect(command, args, **kwargs):
        async def ping():
            if healthy:
                return
            entered.add(command)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(command)

        kwargs["_on_session"](SimpleNamespace(send_ping=ping))
        yield []

    monkeypatch.setattr(pi_python.mcp, "connect_stdio", connect)

    def setup(api):
        for name in ("one", "two"):
            api.add_mcp_server(name, {"command": name})

    async with load_plugins(Plugin("p", setup)) as plugins:
        status = await asyncio.wait_for(
            plugins.readiness(required_mcp_servers=["one", "two"], mcp_timeout=0.01), 1
        )
        assert status.missing_mcp_servers == ("one", "two")
        assert entered == stopped == {"one", "two"}
        healthy = True
        assert (await plugins.readiness(required_mcp_servers=["one", "two"])).ready
        healthy = False
        entered.clear()
        stopped.clear()
        task = asyncio.create_task(plugins.readiness())
        async with asyncio.timeout(1):
            while len(entered) < 2:
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == {"one", "two"}


async def test_tool_business_error_does_not_fail_readiness(metadata_server):
    def setup(api):
        api.add_mcp_server("business", metadata_server)

    async def emit(value):
        pass

    async with load_plugins(Plugin("p", setup)) as plugins:
        failing = next(t for t in plugins.tools if t.name.endswith("__fail"))
        result = await failing.execute(
            {"reason": "business validation failed"},
            ToolContext("run", "call", CancelToken(), emit),
        )
        assert result.is_error
        assert (await plugins.readiness(required_mcp_servers=["business"])).ready


# --- unused imports guard ---------------------------------------------------------------


def test_public_names_are_exported():
    import pi_python.plugins as plugins

    assert set(plugins.__all__) <= set(dir(plugins))
    assert AgentConfigUpdate and ToolResult  # imported for the readers of these tests


async def test_import_errors_name_the_plugin(tmp_path):
    write(tmp_path / "broken" / "plugin.py", "import no_such_module_for_pi_tests\n")
    with pytest.raises(ModuleNotFoundError) as info:
        async with load_plugins(tmp_path / "broken"):
            pass
    assert "while importing plugin 'broken'" in "".join(info.value.__notes__)


def test_parallel_answers_are_capped_like_pi():
    from pi_python.plugins._subagents import PER_TASK_OUTPUT_CAP, _truncate

    short = "é" * 10
    assert _truncate(short) == short
    long = "é" * PER_TASK_OUTPUT_CAP  # two bytes each
    capped = _truncate(long)
    kept = capped.split("\n\n[Output truncated")[0]
    assert len(kept.encode("utf-8")) == PER_TASK_OUTPUT_CAP
    assert capped.endswith(
        f"[Output truncated: {PER_TASK_OUTPUT_CAP} bytes omitted. Full output preserved in"
        " tool details.]"
    )


async def test_registrations_must_be_callable():
    for register in (
        lambda api: api.on("finish_turn", "end"),
        lambda api: api.subscribe(None),
        lambda api: api.add_check(True),
        lambda api: api.on_close(42),
    ):
        with pytest.raises(ConfigurationError, match="must be callable"):
            async with load_plugins(Plugin("p", register)):
                pass


async def test_bad_mcp_json_entry_is_skipped_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("PI_PLUGIN_TEST_UNSET", raising=False)
    mcp_plugin(
        tmp_path / "p",
        {
            "needs-token": {
                "url": "https://x.org/mcp",
                "headers": {"A": "${PI_PLUGIN_TEST_UNSET}"},
            },
            "fine": {"command": "x", "enabled": False},
        },
    )
    with pytest.warns(PluginWarning, match="PI_PLUGIN_TEST_UNSET is not set"):
        async with load_plugins(tmp_path / "p") as plugins:
            assert list(plugins._apis[0]._mcp) == ["fine"]


async def test_one_failing_parallel_task_does_not_sink_the_others():
    def setup(api):
        for name in ("good", "bad"):
            provider = ScriptedProvider([AssistantMessage.text(f"{name} done")])
            api.add_agent(AgentDefinition(name, name, provider=provider, tools=[]))

    async def emit(value):
        if value["agent"] == "bad":
            raise RuntimeError("progress display broke")

    async with load_plugins(Plugin("p", setup)) as plugins:
        tool = plugins.subagent_tool()
        context = ToolContext("run", "call", CancelToken(), emit)
        tasks = [{"agent": "good", "task": "a"}, {"agent": "bad", "task": "b"}]
        result = await tool.execute({"tasks": tasks}, context)
    text = result.content[0].text
    assert text.startswith("Parallel: 1/2 succeeded")
    assert "good done" in text
    assert "Agent failed: RuntimeError: progress display broke" in text


async def test_skill_search_visits_each_directory_once(tmp_path, monkeypatch):
    import pi_python.plugins._resources as resources

    skills = tmp_path / "p" / "skills"
    write(skills / "group" / "real" / "SKILL.md", "---\nname: real\ndescription: Real.\n---\n")
    try:
        # Two links back up make the search tree grow exponentially without a guard.
        (skills / "group" / "back").symlink_to(skills, target_is_directory=True)
        (skills / "group" / "again").symlink_to(skills, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available here")
    reads = []
    original = resources.load_skill_file
    monkeypatch.setattr(
        resources, "load_skill_file", lambda *args: reads.append(args[0]) or original(*args)
    )
    async with load_plugins(tmp_path / "p") as plugins:
        assert [s.name for s in plugins.skills] == ["real"]
    assert len(reads) == 1


async def test_entry_point_in_a_submodule_uses_the_package_directory(
    tmp_path, monkeypatch, fresh_demo_package
):
    site = tmp_path / "site"
    make_distribution(site)
    write(site / "demo_plugin" / "hooks.py", "def setup(api):\n    api.add_system_prompt('sub')\n")
    write(
        site / "demo_plugin-0.3.dist-info" / "entry_points.txt",
        "[pi_python.plugins]\ndemo-sub = demo_plugin.hooks:setup\n",
    )
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    async with load_plugins("demo-sub") as plugins:
        assert plugins.plugins[0].root == (site / "demo_plugin").resolve()
        assert [s.name for s in plugins.skills] == ["demo"]


async def test_async_on_error_is_awaited_and_its_own_failure_is_contained(caplog):
    reported = []

    async def on_error(failure):
        reported.append(failure.plugin)
        raise RuntimeError("the reporter itself broke")

    def broken(api):
        api.on("after_tool_call", lambda c, r, ctx: 1 / 0)

    provider = ScriptedProvider([calls(("echo", {"text": "hi"})), AssistantMessage.text("ok")])
    async with load_plugins(Plugin("broken", broken), on_error=on_error) as plugins:
        result = await plugins.agent(provider=provider, tools=[echo]).prompt("go")
    assert result.status == "completed"
    assert result.tool_outcomes[0].result.content[0].text == "hi"
    assert reported == ["broken"]
    assert "on_error raised while reporting" in caplog.text


@pytest.mark.parametrize(
    "slot, bad",
    [
        ("transform_context", lambda messages, cancel: None),
        ("convert_to_llm", lambda messages: "not a list"),
        ("before_tool_call", lambda call, args, context: "yes"),
        ("after_tool_call", lambda call, result, context: "replaced"),
        ("prepare_request", lambda context, cancel: "update"),
        ("finish_turn", lambda context, cancel: "stop"),
    ],
)
async def test_application_hooks_behave_the_same_with_plugins(slot, bad):
    """A wrong value from the application's own hook has the core's effect, whether or not
    a plugin also handles that slot."""

    async def run(with_plugin):
        def quiet(api):
            noop = {
                "transform_context": lambda messages, cancel: None,
                "convert_to_llm": lambda messages: None,
                "before_tool_call": lambda call, args, context: None,
                "after_tool_call": lambda call, result, context: None,
                "prepare_request": lambda context, cancel: None,
                "finish_turn": lambda context, cancel: None,
            }[slot]
            api.on(slot, noop)

        provider = ScriptedProvider(
            [calls(("echo", {"text": "hi"})), AssistantMessage.text("ok")] * 2
        )
        sources = [Plugin("quiet", quiet)] if with_plugin else []
        async with load_plugins(sources) as plugins:
            agent = plugins.agent(provider=provider, tools=[echo], hooks=Hooks(**{slot: bad}))
            try:
                result = await agent.prompt("go")
            except Exception as exc:
                return ("raised", type(exc).__name__)
            outcome = result.tool_outcomes[0].result if result.tool_outcomes else None
            return (result.status, outcome and outcome.error_code)

    assert await run(with_plugin=True) == await run(with_plugin=False)


async def test_subagent_uses_the_main_agents_current_model():
    def setup(api):
        api.add_agent(AgentDefinition("helper", "Helps", tools=[]))

        @api.on("prepare_request")
        def switch(context, cancel):
            # Only the main conversation (prompt "go") switches; the subagent must inherit it.
            if any(getattr(m, "content", None) == "go" for m in context.messages):
                return TurnUpdate(model="switched-model")
            return None

    provider = ScriptedProvider(
        [
            delegating(("agent", "helper"), ("task", "help")),
            AssistantMessage.text("helped"),
            AssistantMessage.text("done"),
        ]
    )
    async with load_plugins(Plugin("p", setup)) as plugins:
        result = await plugins.agent(provider=provider, model="first-model").prompt("go")
    assert result.status == "completed"
    assert [r.model for r in provider.requests] == ["switched-model"] * 3


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "//host/share/x",
        "\\\\host\\share\\x",
        "C:\\Windows\\x",
        "C:x",
        "../x",
        "a/../../x",
    ],
)
async def test_read_skill_rejects_paths_outside_before_touching_them(tmp_path, path):
    root = lab_plugin(tmp_path / "lab")
    async with load_plugins(root) as plugins:
        result = await plugins.skill_tool().execute({"name": "dedup", "path": path}, None)
    assert result.is_error and "outside the skill directory" in result.content[0].text


async def test_read_skill_caps_skill_md_too(tmp_path):
    from pi_python.plugins import MAX_SKILL_FILE_BYTES

    root = tmp_path / "p"
    write(
        root / "skills" / "big" / "SKILL.md",
        "---\nname: big\ndescription: Big.\n---\n" + "x" * MAX_SKILL_FILE_BYTES,
    )
    async with load_plugins(root) as plugins:
        result = await plugins.skill_tool().execute({"name": "big"}, None)
    assert result.is_error and "larger than 256 KiB" in result.content[0].text


@pytest.mark.skipif(not HAS_MCP, reason="needs the [mcp] extra")
async def test_connect_http_does_not_follow_a_redirect_to_another_origin():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from pi_python.mcp import connect_http

    received = []

    class Elsewhere(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.headers.get("X-Api-Key"))
            self.send_response(500)
            self.end_headers()

        do_GET = do_DELETE = do_POST

        def log_message(self, *args):
            pass

    elsewhere = HTTPServer(("127.0.0.1", 0), Elsewhere)

    class Redirect(Elsewhere):
        def do_POST(self):
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{elsewhere.server_port}/mcp")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_DELETE = do_POST

    origin = HTTPServer(("127.0.0.1", 0), Redirect)
    for server in (elsewhere, origin):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(Exception):
            async with connect_http(
                f"http://127.0.0.1:{origin.server_port}/mcp",
                headers={"X-Api-Key": "sk-test-secret"},
                init_timeout=5,
            ):
                pass
    finally:
        origin.shutdown()
        elsewhere.shutdown()
    assert "sk-test-secret" not in received


async def test_mcp_secrets_stay_out_of_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_PLUGIN_TEST_TOKEN", "sk-very-secret\n")
    root = mcp_plugin(
        tmp_path / "p",
        {
            "docs": {
                "url": "http://127.0.0.1:9/mcp",
                "headers": {"X-Key": "${PI_PLUGIN_TEST_TOKEN}"},
            }
        },
    )
    with pytest.warns(PluginWarning, match="header 'X-Key' contains a control character"):
        async with load_plugins(root) as plugins:
            pass
    assert not any("sk-very-secret" in d for d in plugins.diagnostics)

    # A connection error that quotes the URL, as SDK 1.x errors do, is reported masked.
    import contextlib

    import pi_python.mcp

    @contextlib.asynccontextmanager
    async def failing(url, **kwargs):
        raise RuntimeError(f"Server error for url {url!r}")
        yield

    monkeypatch.setattr(pi_python.mcp, "connect_http", failing)
    monkeypatch.setenv("PI_PLUGIN_TEST_TOKEN", "sk-very-secret")
    root = mcp_plugin(
        tmp_path / "q",
        {"docs": {"url": "https://x.org/mcp?key=${PI_PLUGIN_TEST_TOKEN}"}},
    )
    with pytest.warns(PluginWarning, match="did not connect"):
        async with load_plugins(root) as plugins:
            pass
    (message,) = [d for d in plugins.diagnostics if "did not connect" in d]
    assert "sk-very-secret" not in message and "key=***" in message


async def test_subagents_follow_the_main_agents_run_limits():
    from pi_python import RunLimits

    busy = ScriptedProvider(
        [calls(("echo", {"text": "1"})), calls(("echo", {"text": "2"})), AssistantMessage.text("x")]
    )

    def setup(api):
        api.add_agent(AgentDefinition("busy", "Busy", provider=busy, tools=["echo"]))

    provider = ScriptedProvider(
        [delegating(("agent", "busy"), ("task", "work")), AssistantMessage.text("done")]
    )
    async with load_plugins(Plugin("p", setup)) as plugins:
        agent = plugins.agent(
            provider=provider, tools=[echo], limits=RunLimits(max_model_requests=2)
        )
        result = await agent.prompt("go")
    assert result.status == "completed"
    outcome = result.tool_outcomes[0].result
    assert outcome.is_error and "limit_reached" in outcome.content[0].text
    assert len(busy.requests) == 2


async def test_cancelled_and_concurrent_plugin_close_finishes_once():
    started, finish = asyncio.Event(), asyncio.Event()
    events = []

    async def slow_close():
        events.append("start")
        started.set()
        await finish.wait()
        events.append("finish")

    def setup(api):
        api.on_close(lambda: events.append("resource closed"))
        api.on_close(slow_close)

    plugins = await load_plugins([Plugin("close-test", setup)]).open()
    first = asyncio.create_task(plugins.aclose())
    await started.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(plugins.aclose())
    await asyncio.sleep(0)
    try:
        assert not first.done()
        with pytest.raises(ConfigurationError):
            plugins.system_prompt()
    finally:
        finish.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None
    await plugins.aclose()
    assert events == ["start", "finish", "resource closed"]


async def test_self_cancelled_plugin_closer_does_not_skip_resources():
    events = []

    async def cancel_close():
        raise asyncio.CancelledError

    def setup(api):
        api.on_close(lambda: events.append("closed"))
        api.on_close(cancel_close)

    plugins = await load_plugins([Plugin("close-test", setup)]).open()
    with pytest.raises(asyncio.CancelledError):
        await plugins.aclose()
    assert events == ["closed"]
    await plugins.aclose()
    assert events == ["closed"]


async def test_cancelled_plugin_close_joins_owned_tasks_before_resources():
    cleaning, finish = asyncio.Event(), asyncio.Event()
    events = []
    scope = None

    def setup(api):
        nonlocal scope
        api.on_close(lambda: events.append("resource"))
        scope = api.task_scope()

    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()
            events.append("task")

    plugins = await load_plugins([Plugin("scope-test", setup)]).open()
    caller = asyncio.create_task(scope.run(work()))
    for _ in range(3):
        await asyncio.sleep(0)
    closer = asyncio.create_task(plugins.aclose())
    await cleaning.wait()
    closer.cancel()
    await asyncio.sleep(0)
    assert events == []
    finish.set()
    results = await asyncio.gather(caller, closer, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert events == ["task", "resource"]
