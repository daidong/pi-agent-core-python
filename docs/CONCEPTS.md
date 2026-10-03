# pi-python on one page

**English** | [中文](zh/CONCEPTS.md)

This library does one thing: it repeatedly sends the conversation to a model, runs the tools the model asks for, and hands the results back to the model, until the model gives an answer. Understand the five concepts below and you can read all of the code.

## Five concepts

| Concept | What it is | Where |
|---|---|---|
| **Transcript** | An append-only list of messages: user input, model answers, tool results, and system messages. System instructions and tool declarations are written as system messages too, so every change made mid-conversation stays in the record | `messages.py`, `transcript.py` |
| **Agent** | One conversation. It holds the transcript, the default configuration (model, options, tools), two input queues and the event subscribers. It runs one prompt at a time | `agent.py`, `queues.py` |
| **Run** | One execution of `prompt` or `continue_run`. It holds that run's cancel token, background tasks and tool-execution records, and packages them as a `RunResult` at the end | `run.py`, `loop.py` |
| **Provider** | A function that takes a request and produces a stream of model events, the last of which is the complete answer. The Agent does not know whether HTTP, WebSocket or a script is behind it | `provider.py`, `providers/` |
| **Tool** | A name, a description, an argument schema and an execute function (async or plain). Usually generated with `@tool` from a type-annotated function. Arguments are strictly validated before execution, and the result is written back to the transcript | `tools.py`, `function_tools.py` |

Model capabilities are not a sixth concept but an input to the Provider: `ModelInfo` records the context length, output limit, reasoning levels and server-side features. When you give a real Provider a model name, it looks the name up in the built-in model table; if the name is missing, it raises an error, and you pass a `ModelInfo` instead. The scripted `ScriptedProvider` needs no model table.

## What happens in one turn

```text
prompt("…")
  │
  ▼
Accept input (queued guidance; tool changes are written as system messages)
  │
  ▼
pre-request hooks ─► transform_context ─► convert_to_llm
  │
  ▼
Provider stream: start → for each content block start/delta/end → done
  │            (events are checked first: blocks must pair up, deltas must land in an open block,
  │             and the final answer must match the finished blocks; otherwise the turn fails and no tool runs)
  ▼
Answer written to the transcript
  │
  ├─ no tool calls ──► check the follow-up queue ──► end if it is empty
  │
  └─ tool calls ──► validate arguments → before_tool_call → execute → after_tool_call
                     │
                     ▼
                  results written in call order ──► finish_turn ──► next turn
```

All hooks are optional. When the model errors or is cancelled, the answer (with whatever was already generated) is recorded in history as usual, just as in Pi; `finish_turn` and `turn_end` run, and then the run ends. The run also ends when it reaches a limit the application has set. `RunResult.status` says why it ended.

## Minimal example

```python
from pi_python import Agent, AssistantMessage, ScriptedProvider, Tool, ToolCall, ToolResult

async def add(args, context):
    return ToolResult.text(str(args["a"] + args["b"]))

provider = ScriptedProvider([
    AssistantMessage([ToolCall("c1", "add", {"a": 2, "b": 3})], "tool_use"),
    AssistantMessage.text("5"),
])
schema = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
          "required": ["a", "b"], "additionalProperties": False}
result = await Agent(provider=provider, tools=[Tool("add", "Add", schema, add)]).prompt("2+3?")
```

To use a real model, change only the Provider and the model name: `Agent(provider=AnthropicProvider(api_key=...), model="claude-sonnet-5-5", ...)`.

## What goes beyond Pi

Pi leaves the following to applications; this library puts them in the core for research and HPC use. Read about them when you need them:

- Strict argument validation, with no automatic conversion of strings to numbers (write `prepare_arguments` if you need it);
- `RunLimits`: the cleanup deadline after cancellation, plus optional caps on model requests, tool calls and tool concurrency, a tool timeout and a total run time (none of these caps is set by default; as in Pi, the application decides);
- When a tool reports that its outcome is "unknown" (for example, the connection dropped after a job was submitted), the run stops and waits for the application to reconcile, with no automatic retry. Cancelling a tool, or a tool timing out, does not trigger this guard; it is recorded as an ordinary error result;
- State returned to callers is always a copy, and configuration updates made during a run take effect at the next turn.

Details are in the [API](API.md); model connectors (including local models) are in [PROVIDERS](PROVIDERS.md); the item-by-item comparison with Pi is in the [coverage map](../compat/COVERAGE.md) (Chinese). Common patterns for building fuller agents all have runnable examples: [sub-agents](../examples/subagent.py), [saving and restoring conversations](../examples/save_restore.py), [compaction on overflow and retry on errors](../examples/recovery.py), [MCP tools](../examples/mcp_tools.py), [local models](../examples/local_model.py).
