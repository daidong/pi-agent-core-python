# Using pi-python-core with a coding agent

**English** | [中文](zh/CODING_AGENTS.md)

Use this guide when building an application **with** this library. Start with the
offline tool call below, then replace its model provider. For a reusable extension,
follow [Plugin development](PLUGIN_DEVELOPMENT.md). Repository maintenance commands
are at the end; they are not required in a consuming application.

## 1. Establish the environment

The distribution is `pi-python-core`; the import is `pi_python`. Python 3.11 or later
is required. The library uses asyncio. Import public APIs from `pi_python`,
`pi_python.providers`, `pi_python.plugins`, or `pi_python.mcp`, rather than private
modules whose names start with `_`.

In a consuming project, install with `uv add pi-python-core` or
`python -m pip install pi-python-core`. Optional extras are `[mcp]` for MCP tools,
`[mcp-interactive]` for host sampling/forms, and `[oauth]` for ChatGPT identity-token
verification. Built-in model connectors do not require an extra.

When working from this checkout, run these commands from the repository root:

```bash
uv sync --locked --extra mcp-interactive
uv run python examples/quickstart.py
uv run python examples/plugin_demo.py
```

These examples are offline and need no credentials. The docs describe the checked-out
code; its version may not yet be published. Inspect `pyproject.toml` and check the
installed version with `python -c "from importlib.metadata import version; print(version('pi-python-core'))"`.
For another project to use this checkout, install it in that project's environment
with `python -m pip install -e /absolute/path/to/pi-python` (append
`[mcp-interactive]` to that path if needed).

## 2. Run a complete offline tool call

Save this as `app.py` and run `python app.py` in the installed environment, or
`uv run python app.py` in this checkout. It prints `3 words.` and checks the actual
tool result, not just the model's scripted answer.

```python
import asyncio

from pi_python import (
    Agent, AssistantMessage, RunLimits, ScriptedProvider, TextContent, ToolCall, tool,
)


@tool
def count_words(text: str) -> dict[str, int]:
    """Count whitespace-separated words in text."""
    return {"words": len(text.split())}


async def main() -> None:
    provider = ScriptedProvider([
        AssistantMessage([
            ToolCall("count-1", "count_words", {"text": "one two three"}),
        ], stop_reason="tool_use"),
        AssistantMessage.text("3 words."),
    ])
    async with Agent(
        provider=provider,
        tools=[count_words],
        limits=RunLimits(max_model_requests=4, max_tool_calls=4, run_timeout=30),
    ) as agent:
        result = await agent.prompt("Count the words in: one two three")
        if result.status != "completed":
            raise RuntimeError(f"{result.status}: {result.errors}")
        outcome = result.tool_outcomes[0]
        assert outcome.execution_status == "succeeded"
        assert outcome.result.structured_content == {"words": 3}
        answer = result.messages[-1]
        assert isinstance(answer, AssistantMessage)
        print("".join(b.text for b in answer.content if isinstance(b, TextContent)))


if __name__ == "__main__":
    asyncio.run(main())
```

`Agent` owns the conversation and tool loop. A `Provider` supplies model responses.
`@tool` turns a typed function and its docstring into a tool declaration. The model
chooses tool names and JSON arguments; the library validates arguments and executes
the functions. `ScriptedProvider` fixes those choices for a deterministic test. It
does not demonstrate that a real model will choose the same tool or answer.

## 3. Replace the provider, keep application ownership explicit

For a real model, pass the application's credentials and chosen model explicitly.
Inside an async function, the replacement looks like this (it makes a paid/network
call and is not part of the offline example):

```python
import os
from pi_python.providers import AnthropicProvider

provider = AnthropicProvider(api_key=os.environ["ANTHROPIC_API_KEY"])
try:
    async with Agent(
        provider=provider,
        model=os.environ["ANTHROPIC_MODEL"],
        tools=[count_words],
        limits=RunLimits(max_model_requests=4, run_timeout=60),
    ) as agent:
        result = await agent.prompt("Count the words in: one two three")
        if result.status != "completed":
            raise RuntimeError(f"{result.status}: {result.errors}")
finally:
    await provider.aclose()
```

`Agent.aclose()` closes the agent's work, not a shared provider. The application
closes its provider after all agents using it are done. Keep creation, use and
cleanup on the same event loop. For another connector, use
[Providers](PROVIDERS.md) and the runnable [local-model example](../examples/local_model.py).

## 4. Observe the contracts when writing application code

| Concern | Contract to follow |
|---|---|
| Async versus sync | In servers/notebooks use `await agent.prompt(...)`. In a plain synchronous script use `agent.prompt_sync(...)`. Do not call a blocking API on an event-loop thread or repeatedly wrap a loop-bound provider in separate `asyncio.run` calls. |
| Conversation ownership | One agent accepts one active run. Await turns in order; use separate agents for independent conversations. `agent.state` is a copy. |
| Completion | Check `result.status`: `completed`, `failed`, `cancelled`, or `limit_reached`. Model/tool failures are not all raised as Python exceptions. Inspect `errors` and `tool_outcomes` before accepting a business result. |
| Tool inputs | Give functions typed parameters and clear docstrings. A `ToolContext` parameter is injected and omitted from the model schema. Arguments are validated without JSON type coercion. |
| Tool outputs | Return JSON-compatible values or `ToolResult`. Dicts/numbers are available as `structured_content` and text. `details` is application data; it is not automatically sent to the model. |
| Concurrency and limits | Tool batches run in parallel by default. Set `RunLimits`; use `@tool(execution_mode="sequential")` for tools that require a sequential batch. Plain functions run in worker threads; cancellation cannot forcibly stop those threads. |
| Cancellation | Use `agent.abort()` and await completion/cleanup. Check `cleanup_complete` and `reconciliation_required` before reusing an agent or retrying a side effect. Prefer async tools for cancellable I/O. |
| Recovery | `continue_run()` can retry a failed model turn without replaying past tools. It is not a general retry mechanism for failed business operations. See [recovery](../examples/recovery.py). |
| Persistence | Store `encode_messages(list(agent.state.messages))`; restore with `decode_messages` and `Agent(messages=...)`. `result.messages` contains only the current run's additions. See [save/restore](../examples/save_restore.py). |
| Streaming | `agent.subscribe(listener)` receives `message_update` events with `delta_type` and `delta`. Critical listeners are awaited, so keep them bounded. See [quickstart](../examples/quickstart.py). |

For application-owned async work use `TaskScope`. To call an existing async connection
from a synchronous tool's worker thread, use `LoopPortal` created on its owning loop.
Do not move that connection to `run_sync`'s separate loop. See
[task ownership and thread calls](API.md#owned-tasks-and-calls-from-worker-threads).
These APIs and feature declarations are part of the 0.10.0 source line; check
`require_features(["task-scope-v1", "loop-portal-v1"])` when depending on them.

## 5. Choose the smallest integration surface

| Need | Start here |
|---|---|
| A few tools in one application | `Agent(..., tools=[...])`; no plugin is necessary. |
| Reusable tools, instructions and resources | [Plugin development workflow](PLUGIN_DEVELOPMENT.md), then [plugin API](PLUGINS.md). |
| Tools from an MCP server | [MCP tools example](../examples/mcp_tools.py); keep the connection open for the agent's lifetime. |
| MCP server requests a host model or form | [MCP interactions](MCP_INTERACTION.md) and [offline example](../examples/mcp_interactive.py). Use the interactive extra and explicit host callbacks. |
| A nested agent | Use `await context.run_agent(child, message)` inside a tool to preserve cleanup ownership and unknown outcomes; see the [subagent example](../examples/subagent.py). Plugins can supply definitions in `agents/`. |
| Your own model adapter | The [Provider contract](API.md#provider); emit exactly one terminal `ModelEvent.done` and honor cancellation. |

MCP incremental sampling is a negotiated extension. It reconstructs the full request
before host policy and model access. It reduces repeated MCP transmission, not the
model's history or token count; business code should not manage its internal cache.

## 6. Verify before handing off

For a consuming application, run an offline tool-call test, assert the actual tool
result, exercise failure/cancellation when relevant, and then test the configured real
provider separately. Report which paths ran. For plugins, also verify the installed
wheel using the [plugin workflow](PLUGIN_DEVELOPMENT.md).

When changing this library itself, read the applicable `AGENTS.md`, follow its scope,
and run the relevant tests. The repository checks are:

```bash
uv run ruff check src tests examples compat scripts
uv run ruff format --check src tests examples compat scripts
uv run mypy src/pi_python
uv run pytest -q
```

Use [API](API.md) for exact contracts, [concepts](CONCEPTS.md) for the execution model,
and [reference verification](../reference/README.md) when a change affects upstream
compatibility. Do not invent methods or assume a different agent framework's APIs apply.
