"""Migrated timing consumers and opt-in application-batch boundaries."""

from copy import copy, deepcopy
from dataclasses import asdict, fields

import pytest
from pi_python import *


# Exact field sets from the published 0.10.1 dataclasses and tool event payloads.
OUTCOME_FIELDS = {
    "call",
    "execution_status",
    "original_arguments",
    "prepared_arguments",
    "raw_result",
    "result",
    "error",
    "_settled",
}
LEGACY_OUTCOME_FIELDS = OUTCOME_FIELDS.copy()
OUTCOME_FIELDS.add("duration_ms")

CONTEXT_FIELDS = {
    "run_id",
    "call_id",
    "cancel",
    "_emit",
    "assistant_message",
    "agent_context",
    "tool_call",
    "args",
    "result",
    "is_error",
    "_on_reconciliation_required",
    "_reconciliation_required",
}
EVENT_FIELDS = {
    "tool_execution_start": {"name", "arguments"},
    "tool_execution_update": {"update"},
    "tool_execution_end": {"result", "execution_status", "duration_ms"},
}


async def test_unmigrated_strict_subscriber_reports_timing_schema_failure():
    async def execute(args, ctx):
        return ToolResult.text("effect completed")

    def legacy(event):
        if event.type == "tool_execution_end":
            assert set(event.data) == {"result", "execution_status"}

    agent = Agent(provider=ScriptedProvider(responses()), tools=[Tool("check", "", {}, execute)])
    agent.subscribe(legacy)
    result = await agent.prompt("go")
    assert result.status == "failed"
    assert result.tool_outcomes[0].execution_status == "succeeded"
    assert set(asdict(result.tool_outcomes[0])) == LEGACY_OUTCOME_FIELDS | {"duration_ms"}
    assert agent.state.diagnostics[-1]["event"] == "tool_execution_end"


def responses():
    return [
        AssistantMessage([ToolCall("call", "check", {})], "tool_use"),
        AssistantMessage.text("done"),
    ]


def reject_custom(messages):
    if any(isinstance(m, CustomMessage) for m in messages):
        raise ValueError("Legacy converter does not support custom messages")
    return messages


async def test_migrated_consumers_keep_exact_fields_and_integer_turns_by_default():
    seen_events = set()
    contexts = []

    async def execute(args, ctx):
        contexts.append(ctx)
        assert ctx.origin == "model" and ctx.source is None
        assert "_origin" not in vars(ctx) and "_source" not in vars(ctx)
        await ctx.emit_update({"progress": 1})
        return ToolResult.text("ok")

    def strict_subscriber(event):
        # Migrated consumers accept timing; model turns are still integers.
        assert event.turn_id + 1 > 0
        if event.type in EVENT_FIELDS:
            assert set(event.data) == EVENT_FIELDS[event.type]
            seen_events.add(event.type)
        assert decode_event(encode_event(event)) == event

    provider = ScriptedProvider(responses())
    agent = Agent(
        provider=provider,
        tools=[Tool("check", "", {}, execute)],
        hooks=Hooks(convert_to_llm=reject_custom),
    )
    agent.subscribe(strict_subscriber)
    result = await agent.prompt("go")
    assert result.status == "completed" and len(provider.requests) == 2
    assert seen_events == EVENT_FIELDS.keys()
    assert {f.name for f in fields(ToolContext)} == CONTEXT_FIELDS
    assert {f.name for f in fields(ToolOutcome)} == OUTCOME_FIELDS
    assert set(asdict(result)["tool_outcomes"][0]) == OUTCOME_FIELDS
    assert set(vars(result.tool_outcomes[0])) == OUTCOME_FIELDS
    assert all(not isinstance(m, CustomMessage) for m in agent.state.messages)
    assert len(contexts) == 1


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("origin", ["model", "application"])
async def test_origin_in_events_requires_explicit_opt_in(enabled, origin):
    async def execute(args, ctx):
        await ctx.emit_update({"progress": 1})
        return ToolResult.text("ok")

    agent = Agent(
        provider=ScriptedProvider(responses()),
        tools=[Tool("check", "", {}, execute)],
        include_tool_origin=enabled,
    )
    events = []
    agent.subscribe(events.append)
    result = (
        await agent.prompt("go")
        if origin == "model"
        else await agent.execute_tools([ToolInvocation("check", {})], source="checks@1")
    )
    assert result.status == "completed"
    for event in events:
        if event.type in EVENT_FIELDS:
            assert set(event.data) == EVENT_FIELDS[event.type] | (
                {"origin", "source"} if enabled else set()
            )
            if enabled:
                assert event.data["origin"] == origin
                assert event.data["source"] == ("checks@1" if origin == "application" else None)
    # Explicit properties remain readable; serialization shape never changes.
    assert result.tool_outcomes[0].origin == origin
    assert set(asdict(result.tool_outcomes[0])) == OUTCOME_FIELDS


async def test_application_origin_survives_copy_without_changing_asdict_fields():
    seen = []

    async def execute(args, ctx):
        copied = copy(ctx)
        seen.append((copied.origin, copied.source))
        return ToolResult.text("ok")

    agent = Agent(tools=[Tool("check", "", {}, execute)])
    events = []
    agent.subscribe(events.append)
    result = await agent.execute_tools([ToolInvocation("check", {})], source="checks@1")
    assert result.status == "completed" and seen == [("application", "checks@1")]
    copied = deepcopy(result.tool_outcomes[0])
    assert (copied.origin, copied.source) == ("application", "checks@1")
    assert set(asdict(copied)) == OUTCOME_FIELDS
    assert result.messages[0].data["calls"][0]["origin"] == "application"
    assert all(set(e.data) == EVENT_FIELDS[e.type] for e in events if e.type in EVENT_FIELDS)


async def test_integer_only_subscriber_fails_only_when_application_path_is_used():
    effects = []

    async def execute(args, ctx):
        effects.append(1)
        return ToolResult.text("ok")

    def integer_only(event):
        return event.turn_id + 1

    agent = Agent(provider=ScriptedProvider(responses()), tools=[Tool("check", "", {}, execute)])
    agent.subscribe(integer_only)
    assert (await agent.prompt("old path")).status == "completed"
    assert effects == [1]
    result = await agent.execute_tools([ToolInvocation("check", {})])
    assert result.status == "failed"
    assert effects == [1]  # subscriber failed at agent_start, before executing anything
    assert "TypeError" in agent.state.diagnostics[-1]["error"]
    assert result.tool_outcomes[0].execution_status == "not_started"


@pytest.mark.parametrize("converter", ["none", "reject", "filter", "passthrough"])
async def test_custom_converter_compatibility_when_new_history_is_introduced(converter):
    async def execute(args, ctx):
        return ToolResult.text("unique-check-feedback")

    def filter_custom(messages):
        return [m for m in messages if not isinstance(m, CustomMessage)]

    callbacks = {
        "none": None,
        "reject": reject_custom,
        "filter": filter_custom,
        "passthrough": lambda messages: messages,
    }
    provider = ScriptedProvider([AssistantMessage.text("old"), AssistantMessage.text("new")])
    agent = Agent(
        provider=provider,
        tools=[Tool("check", "", {}, execute)],
        hooks=Hooks(convert_to_llm=callbacks[converter]),
    )
    assert (await agent.prompt("old path")).status == "completed"
    batch = await agent.execute_tools([ToolInvocation("check", {})])
    assert batch.status == "completed" and len(provider.requests) == 1
    result = await agent.prompt("interpret check")
    if converter == "reject":
        assert result.status == "failed" and len(provider.requests) == 1
        assert "Legacy converter" in str(result.errors)
    else:
        assert result.status == "completed" and len(provider.requests) == 2
        feedback = [
            m for m in provider.requests[-1].messages if "unique-check-feedback" in str(asdict(m))
        ]
        # Filtering all custom messages silently removes the feedback; core cannot recover it.
        assert bool(feedback) is (converter != "filter")
    # Conversion never rewrites committed history.
    assert any(isinstance(m, CustomMessage) for m in agent.state.messages)


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_origin_opt_in_is_boolean(value):
    with pytest.raises(ConfigurationError, match="include_tool_origin"):
        Agent(include_tool_origin=value)
