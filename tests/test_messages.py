import json
import pytest
from pi_python import *


def test_C25_roundtrip_and_no_execution():
    messages = [
        SystemMessage("base", {"a": "one"}, [ToolDeclaration("t", "", {"type": "object"})]),
        UserMessage("hi"),
        AssistantMessage([ToolCall("x", "t", {})], "tool_use"),
        ToolResultMessage("x", "t", [TextContent("ok")]),
        CustomMessage("note", {"ok": True}),
    ]
    assert decode_messages(encode_messages(messages)) == messages
    for bad in [float("nan"), float("inf"), object(), {1: "x"}]:
        with pytest.raises(MessageValidationError):
            encode_messages([CustomMessage("x", bad)])
    for version in [0, 1, 2, 4, True, "3"]:
        with pytest.raises(MessageValidationError):
            decode_messages(json.dumps({"schema_version": version, "messages": []}))


def test_C25_history_rejects_dangling_duplicate_and_unmatched():
    call = AssistantMessage([ToolCall("x", "t", {})], "tool_use")
    for history in [
        [call],
        [ToolResultMessage("x", "t", [])],
        [call, UserMessage("oops")],
        [AssistantMessage([ToolCall("x", "t", {}), ToolCall("x", "t", {})])],
        [AssistantMessage([ToolCall("x", "t", {})], "error")],
    ]:
        with pytest.raises(MessageValidationError):
            Agent(provider=ScriptedProvider([]), messages=history)


def test_C12_system_replay():
    t = ToolDeclaration("t", "", {"type": "object"})
    messages = [
        SystemMessage("base", {"first": "one", "gone": "two"}, [t]),
        SystemMessage("more", {"first": "new", "gone": None, "last": "three"}, tools_removed=["t"]),
    ]
    assert current_system_prompt(messages) == "base\n\nmore\n\nnew\n\nthree"
    assert current_tools(messages) == []
    assert current_system_message(messages).timestamp == messages[0].timestamp


def test_C24_unsupported_input():
    for data in [
        {"role": "user", "content": [{"type": "image", "url": "x"}]},
        {"role": "assistant", "content": [{"type": "thinking", "text": "x"}]},
        {"role": "assistant", "content": [], "stop_reason": "future"},
    ]:
        with pytest.raises(MessageValidationError):
            message_from_dict(data)


def test_events_codec():
    e = Event("agent_start", "r", 1, 1, data={"x": [1]})
    assert decode_event(encode_event(e)) == e
    with pytest.raises(MessageValidationError):
        decode_event('{"schema_version":2}')
