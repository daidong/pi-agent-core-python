"""Native mid-conversation changes, request parity helpers and estimates."""

import json

import httpx
from pi_python import *
from pi_python.estimate import clamp_max_tokens_to_context, estimate_context_tokens, short_hash
from pi_python.providers import AnthropicProvider, DeepSeekProvider, HTTPTransport, OpenAIProvider
from pi_python.providers.common import transform_messages
from pi_python.transcript import (
    has_non_additive_tool_changes,
    has_tool_redefinitions,
    render_system_update,
    resolve_transcript,
    resolve_transcript_tools,
)

from tests.test_providers import events, remote

SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


async def noop(args, context):
    return ToolResult.text("ok")


def tool(name):
    return Tool(name, f"{name} tool", SCHEMA, noop)


def declaration(name, description=None):
    return ToolDeclaration(name, description or f"{name} tool", SCHEMA)


async def test_claude_agent_tool_and_effort_changes_use_native_blocks():
    provider, requests, _, client = remote(
        "anthropic", [events("anthropic-text"), events("anthropic-text")]
    )
    async with client:
        agent = Agent(
            provider=provider,
            model="claude-opus-5-5",
            system_prompt="base",
            tools=[tool("alpha")],
            thinking_level="low",
        )
        first = await agent.prompt("one")
        agent.update_config(AgentConfigUpdate(tools=[tool("alpha"), tool("beta")]))
        agent.update_config(AgentConfigUpdate(options={"reasoning": "high"}))
        await agent.prompt("two")
    assert first.messages[-1].provider_thinking_level == "low"
    one, two = (json.loads(r.content) for r in requests)
    assert [t["name"] for t in one["tools"]] == ["alpha", "__pi_deferred_placeholder__"]
    assert [(t["name"], t.get("defer_loading")) for t in two["tools"]] == [
        ("alpha", None),
        ("__pi_deferred_placeholder__", True),
        ("beta", True),
    ]
    # The request-level tool list only grows, so the cached prefix survives the change.
    assert two["tools"][:2] == one["tools"]
    roles = [(m["role"], m.get("output_config")) for m in two["messages"]]
    assert roles == [
        ("user", None),
        ("system", {"effort": "low"}),
        ("assistant", None),
        ("user", None),
        ("system", None),
        ("system", {"effort": "high"}),
    ]
    change = two["messages"][4]["content"]
    assert [b["type"] for b in change] == ["tool_addition"]
    assert change[0]["tool"] == {"type": "tool_reference", "name": "beta"}
    beta = requests[1].headers["anthropic-beta"].split(",")
    assert "mid-conversation-tool-changes-2026-07-01" in beta
    assert "mid-conversation-output-config-2026-07-01" in beta
    assert two["output_config"] == {"effort": "high"}


async def test_claude_without_native_support_folds_changes_into_system():
    provider, requests, _, client = remote(
        "anthropic", [events("anthropic-text"), events("anthropic-text")]
    )
    async with client:
        agent = Agent(
            provider=provider, model="claude-sonnet-4-5", system_prompt="base", tools=[tool("a")]
        )
        await agent.prompt("one")
        agent.update_config(AgentConfigUpdate(tools=[tool("b")]))
        await agent.prompt("two")
    body = json.loads(requests[1].content)
    assert [t["name"] for t in body["tools"]] == ["b"]
    assert {m["role"] for m in body["messages"]} == {"user", "assistant"}
    assert body["thinking"] == {"type": "disabled"}
    assert "anthropic-beta" not in requests[1].headers


async def test_openai_agent_anchors_added_tools_and_updates_in_place():
    provider, requests, _, client = remote("openai", [events("openai-text"), events("openai-text")])
    provider.api_key = "sk-test"  # an API key, not a ChatGPT sign-in token
    async with client:
        agent = Agent(provider=provider, model="gpt-5.5", system_prompt="base", tools=[tool("a")])
        await agent.prompt("one")
        agent.update_config(AgentConfigUpdate(tools=[tool("a"), tool("b")]))
        await agent.prompt([SystemMessage("new guidance"), UserMessage("two")])
    body = json.loads(requests[1].content)
    assert [t["name"] for t in body["tools"]] == ["a"]
    kinds = [item.get("type", item.get("role")) for item in body["input"]]
    assert kinds == ["developer", "user", "message", "additional_tools", "developer", "user"]
    assert body["input"][3]["tools"][0]["name"] == "b"
    assert body["input"][4]["content"] == "new guidance"
    assert body["max_output_tokens"] == 128000


async def test_chatgpt_sign_in_omits_fields_the_service_rejects():
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=b"".join(_sse(events("openai-text"))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(api_key="oauth-access-token", transport=HTTPTransport(client))
        await Agent(
            provider=provider,
            model="gpt-5.5",
            options={"cache_retention": "long", "temperature": 0.5, "session_id": "s"},
        ).prompt("go")
    assert seen[0]["prompt_cache_key"] == "s"
    for field in ("prompt_cache_retention", "max_output_tokens", "temperature"):
        assert field not in seen[0]


def _sse(values):
    for v in values:
        yield f"event: {v['type']}\ndata: {json.dumps(v)}\n\n".encode()


def test_system_update_rendering_and_transcript_resolution():
    head = SystemMessage("base", {"rules": "old", "docs": "doc"}, [declaration("a")], timestamp=0)
    update = SystemMessage(
        "more", {"rules": "new", "docs": None}, [declaration("b")], ["a"], timestamp=2
    )
    messages = [head, UserMessage("u", timestamp=1), update]
    assert render_system_update(update) == (
        'more\n\nUpdated system prompt section "rules":\n\nnew\n\n'
        'Removed system prompt section "docs".'
    )
    collapsed = resolve_transcript(messages, False)
    assert [m.role for m in collapsed] == ["system", "user"]
    assert current_system_prompt(collapsed) == "base\n\nmore\n\nnew"
    assert resolve_transcript(messages, True) == messages
    assert has_non_additive_tool_changes(messages) and not has_tool_redefinitions(messages)
    tools, anchors = resolve_transcript_tools(messages, True)
    assert not anchors and [t.name for t in tools] == ["b"]
    additive = [head, SystemMessage(tools_added=[declaration("b")], timestamp=2)]
    tools, anchors = resolve_transcript_tools(additive, True)
    assert anchors and [t.name for t in tools] == ["a"]
    redefined = [head, SystemMessage(tools_added=[declaration("a", "changed")], timestamp=2)]
    assert has_tool_redefinitions(redefined)


def test_replay_skips_failed_turns_and_holds_system_after_results():
    call = ToolCall("c1", "a", {})
    messages = [
        UserMessage("u"),
        AssistantMessage.text("broken", stop_reason="error", provider="x", api="y", model="z"),
        AssistantMessage([call], "tool_use", provider="x", api="y", model="z"),
        SystemMessage("between"),
        ToolResultMessage("c1", "a", [TextContent("r")]),
    ]
    result = transform_messages(messages, "x", "y", "z")
    assert [m.role for m in result] == ["user", "assistant", "tool_result", "system"]
    orphan = transform_messages(messages[:3], "x", "y", "z")
    assert orphan[-1].role == "tool_result" and orphan[-1].is_error


def test_estimate_matches_pi_heuristic_and_clamps_output():
    assert short_hash("fc_abc|item") == "a6gwzj2fm3s3"  # upstream shortHash output
    assert short_hash("中文😀") == "1uqnrlm1d9i9jc"
    reply = AssistantMessage.text("x", usage={"total_tokens": 900}, timestamp=2)
    messages = [UserMessage("a" * 400, timestamp=1), reply, UserMessage("b" * 8, timestamp=3)]
    assert estimate_context_tokens(messages) == 900 + 2
    assert estimate_context_tokens([messages[0]]) == 100
    assert clamp_max_tokens_to_context(10_000, messages, 128_000) == 10_000 - 902 - 4096
    assert clamp_max_tokens_to_context(1_000, messages, 50) == 1
    assert clamp_max_tokens_to_context(0, messages, 50) == 50


async def test_claude_output_limit_leaves_room_for_the_estimated_prompt():
    provider = AnthropicProvider(api_key="fixture")
    # 3,583,616 characters estimate to 895,904 tokens of a 1,000,000-token window.
    body = provider.build_request(
        ModelRequest([UserMessage("x" * 3_583_616)], model="claude-opus-5-5")
    )
    assert body["max_tokens"] == 1_000_000 - 895_904 - 4096
    overflow = provider.build_request(
        ModelRequest([UserMessage("x" * 4_000_000)], model="claude-opus-5-5")
    )
    assert overflow["max_tokens"] == 1


async def test_every_responses_profile_streams_through_its_own_request_builder():
    """Regression: a subclass signature drift once failed every DeepSeek request locally."""
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=b"".join(_sse(events("openai-text"))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekProvider(api_key="deepseek-key", transport=HTTPTransport(client))
        result = await Agent(provider=provider, model="deepseek-flash", system_prompt="s").prompt(
            "go"
        )
    assert result.status == "completed"
    assert seen[0]["instructions"] == "s" and "max_output_tokens" not in seen[0]
