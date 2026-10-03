"""Maintained deterministic input fixtures, never expected outputs."""

import json
from pathlib import Path


def text(s="done"):
    return {"role": "assistant", "content": [{"type": "text", "text": s}], "stop_reason": "stop"}


def call(id="x", name="t", args=None):
    return {"type": "tool_call", "id": id, "name": name, "arguments": args or {}}


def use(*calls, reason="tool_use"):
    return {"role": "assistant", "content": list(calls), "stop_reason": reason}


def tool(name="t", **kw):
    return {"name": name, "result": name + "-result", **kw}


def op(method, text, event="tool_execution_end"):
    return {"event": event, "method": method, "message": {"role": "user", "content": text}}


fixtures = {
    "C01-text": {"responses": [text("Hello back")], "prompt": "Hello"},
    "C02-tool": {"tools": [tool()], "responses": [use(call()), text()]},
    "C03-sequential": {
        "tools": [tool("a"), tool("b")],
        "execution_mode": "sequential",
        "responses": [use(call("a", "a"), call("b", "b")), text()],
    },
    "C04-parallel": {
        "tools": [tool("a", wait_for="b"), tool("b")],
        "responses": [use(call("a", "a"), call("b", "b")), text()],
    },
    "C05-one-sequential": {
        "tools": [tool("a"), tool("b", execution_mode="sequential")],
        "responses": [use(call("a", "a"), call("b", "b")), text()],
    },
    "C06-one-failure": {
        "tools": [tool("a", error="boom"), tool("b")],
        "responses": [use(call("a", "a"), call("b", "b")), text()],
    },
    "C07-unknown": {"responses": [use(call("x", "missing")), text()]},
    "C08-prepare": {
        "tools": [
            tool(
                input_schema={
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                    "required": ["n"],
                },
                prepare_number="n",
            )
        ],
        "responses": [use(call(args={"n": "3"})), text()],
    },
    "C09-block": {"tools": [tool()], "block_ids": ["x"], "responses": [use(call()), text()]},
    "C09-replace": {
        "tools": [tool(structured_content={"old": 1})],
        "after_text": "replaced",
        "responses": [use(call()), text()],
    },
    "C10-length": {
        "tools": [tool()],
        "responses": [use(call("a"), call("b"), reason="length"), text()],
    },
    "C11-transform": {"transform": True, "responses": [text()]},
    "C12-system": {
        "history": [{"role": "system", "content": "base", "sections": {"a": "one", "b": "two"}}],
        "prompt": {"role": "system", "content": "more", "sections": {"a": "new", "b": None}},
        "responses": [text()],
    },
    "C13-steering": {
        "tools": [tool()],
        "operations": [op("steer", "guide1"), op("steer", "guide2")],
        "responses": [use(call()), text("1"), text("2")],
    },
    "C14-followup-all": {
        "follow_up_mode": "all",
        "operations": [
            op("follow_up", "later1", "turn_end"),
            op("follow_up", "later2", "turn_end"),
        ],
        "responses": [text("1"), text("2")],
    },
    "C14-followup-one": {
        "operations": [
            op("follow_up", "later1", "turn_end"),
            op("follow_up", "later2", "turn_end"),
        ],
        "responses": [text("1"), text("2"), text("3")],
    },
    "C15-prepare": {
        "tools": [tool()],
        "prepare_model": "run-model",
        "next_message": "next input",
        "responses": [use(call()), text()],
    },
    "C16-mixed": {
        "tools": [tool("a", terminate=True), tool("b")],
        "responses": [use(call("a", "a"), call("b", "b")), text()],
    },
    "C16-terminate": {"tools": [tool(terminate=True)], "responses": [use(call())]},
    "C16-continue": {
        "tools": [tool()],
        "finish": ["continue", None],
        "responses": [use(call()), text()],
    },
    "C16-end": {
        "tools": [tool()],
        "finish": ["end"],
        "operations": [op("follow_up", "remain")],
        "responses": [use(call())],
    },
}


def failed(text, reason, error):
    """A response that streams `text`, then fails (error) or waits for abort (aborted)."""
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": reason,
        "error": error,
        "stream": True,
    }


# Failure and abort paths: the provider's failed message is the record, and the turn
# still ends through finishTurn and turn_end (agent-loop.ts streamAssistantResponse/runLoop).
failure_fixtures = {
    "C24-provider-error": {
        "finish": [],
        "responses": [failed("partial answer", "error", "overloaded")],
    },
    "C19-abort-stream": {
        "finish": [],
        "operations": [{"event": "message_update", "method": "abort", "delta_type": "text_delta"}],
        "responses": [failed("partial answer", "aborted", "Request aborted")],
    },
    "C19-abort-tools": {
        "tools": [tool("a", wait_abort=True), tool("b")],
        "finish": [],
        "operations": [{"event": "tool_execution_end", "method": "abort"}],
        "responses": [use(call("a", "a"), call("b", "b"))],
    },
    "C09-before-error": {
        "tools": [tool()],
        "before_error": {"x": "denied"},
        "responses": [use(call()), text()],
    },
}
sources = dict.fromkeys(
    failure_fixtures, "packages/agent/src/agent-loop.ts; packages/agent/src/agent.ts"
)
for name, f in {**fixtures, **failure_fixtures}.items():
    f = {
        "id": name,
        "classification": "mapped",
        "source": sources.get(
            name, "packages/agent/test/agent-loop.test.ts; packages/agent/test/agent.test.ts"
        ),
        "prompt": "go",
        **f,
    }
    Path(f"compat/fixtures/{name}.json").write_text(json.dumps(f, indent=2) + "\n")
