# Python API

**English** | [中文](zh/API.md)

Import the public types from `pi_python`. The minimum Python version is 3.11, and only the `asyncio` backend is used. Read [the one-page concepts](CONCEPTS.md) first. `Agent` makes model requests through a Provider; it never reads keys implicitly and never closes a shared Provider that the caller passed in. Real model connections are covered in [model connectors](PROVIDERS.md).

## Agent and run results

`Agent(provider=..., model="mock" or ModelInfo(...), options={}, system_prompt="", tools=[], messages=[], hooks=Hooks(), limits=RunLimits(), execution_mode="parallel", steering_mode="one_at_a_time", follow_up_mode="one_at_a_time")`. If there is neither a `provider` nor a default stream set with `set_default_stream_fn`, `prompt` raises `ConfigurationError` immediately.

| Method | Behavior |
|---|---|
| `await prompt(str_or_message_or_list)` | Appends the input and runs; returns a `RunResult` |
| `await continue_run()` | Continues from a valid user/tool-result history, or consumes input queued after the final answer; if the last answer failed or was cancelled, retries that turn (see "Recovering from failures"); never replays past tools |
| `prompt_sync(...)` / `continue_run_sync()` | Blocking versions for plain scripts; Ctrl+C cancels the current run; see "Using it from plain scripts" |
| `steer(message)` | Accepts guidance at the next safe boundary; also takes a string |
| `follow_up(message)` | Accepts follow-up input once tools and guidance no longer ask to continue |
| `abort(reason="requested")` | Requests cancellation and returns immediately; the run then wraps up the way Pi does, see "Cancellation, timeouts and events" below |
| `await wait_for_idle()` | Waits for local tasks and the critical end-of-run subscribers; raises if cleanup times out |
| `subscribe(listener)` | Registers a sync or async critical subscriber; returns an unsubscribe function |
| `update_config(AgentConfigUpdate(...))` | Replaces tools, model or options field by field; during a run, the change waits for the next turn boundary |
| `clear_queues(steering=True, follow_up=True)` | Explicitly clears the given queues |
| `await aclose()` | Requests cancellation and waits for this instance's cleanup; does not close a shared Provider |

One instance does not accept overlapping `prompt` / `continue_run` calls; the second call raises `AgentBusyError` immediately. `async with Agent(...)` is supported. `state` returns a defensive copy; changing it does not change the history.

`RunResult` contains `status` (`completed` / `failed` / `cancelled` / `limit_reached`), the `messages` added by this run, the accumulated numeric `usage`, `stop_reason`, `errors`, `tool_outcomes`, `reconciliation_required`, `cleanup_complete`, and the number of items left in each queue. Usage only sums the top-level numeric fields the Provider reports; it does not estimate cost.

`tool_outcomes` records, in call order, the raw arguments, the prepared arguments, `raw_result` and the final `result`. `execution_status` is `not_started`, `running`, `succeeded`, `failed`, `cancelled` or `unknown`. `cancelled` means the tool was cancelled or timed out; `unknown` only comes from a `ToolOutcomeUnknownError` that the tool raised itself. If execution succeeded but the output check or the after-hook failed, the status is still `succeeded` and the final result is an error. No error triggers an automatic retry.

## Provider

```python
class MyProvider:
    async def stream(self, request, cancel):
        cancel.raise_if_cancelled()
        yield ModelEvent.boundary("start", 0, TextContent(""))
        yield ModelEvent.text("hello")
        yield ModelEvent.boundary("end", 0, TextContent("hello"))
        yield ModelEvent.done(AssistantMessage.text("hello", provider="my-provider", model=request.model))
```

`stream` returns an async iterator; the caller does not await it first. `ModelRequest` contains `messages`, the replayed `tools`, `model` (the name), the caller-supplied `model_info` (may be None) and `options`. These are copies, isolated from the run configuration: a Provider may change its own copy but cannot change the history through it.

There is a single event protocol, the same as upstream: an optional `start`; for each content block, in order, `*_start`, any number of `*_delta`, then `*_end` (block types are text, thinking and toolcall); and finally exactly one `done`, carrying the complete answer. The constructors are `ModelEvent.boundary("start"|"end", index, block)`, `ModelEvent.text(delta, index)`, `ModelEvent.thinking(delta, index)`, `ModelEvent.toolcall(json_fragment, index)` and `ModelEvent.done(message)`; `index` is the block's position in the final answer. A non-streaming Provider may emit only `done`. To fail, a Provider can raise an exception or emit an `error` event (all remote Providers do the latter). Either way, the Agent does what Pi does: it records the answer as a message with `stop_reason="error"`, keeping the partial content already streamed, the provider and model names and the error text, and then calls `finish_turn` and emits `turn_end` as usual.

Every event passes the same check: blocks must pair up, deltas must land in an open block of the same type, `done` must be the last event and the iterator must end normally, and the final answer must match the finished blocks (the only allowed addition is upstream's completion of encrypted reasoning content). If the check fails, the turn fails and no tool runs. The usual stop reasons are `stop`, `tool_use`, `length`, `error` and `aborted`.

Message data also allows `pending` and `deferred`. `pending` is only for in-stream snapshots and cannot be committed to history; `deferred` can hold a background handle, but the library does not yet poll background tasks. For `length`, every tool call gets a not-executed result, and the model may then handle the error. `error` / `aborted` messages cannot declare tool calls: before the message is committed, its tool calls are removed, the rest of the partial content is kept, and a `removed_tool_calls` entry is added to `diagnostics`.

## Tool

`Tool(name, description, input_schema, execute, output_schema=None, execution_mode="parallel", prepare_arguments=None)`.

`execute(args, context)` can be an async function or a plain function. Plain functions run in a worker thread so they do not block the event loop. Threads cannot be interrupted: after cancellation, the library waits for the function until the cleanup deadline and uses its result if it finishes; past the deadline, it is treated as a tool that could not be stopped (see "Cancellation, timeouts and events"). Long-running tools are better written as async functions. `ToolContext` provides `run_id`, `call_id`, `cancel` and `await emit_update(json_value)`.

A tool can return a `ToolResult` or a plain value: a string becomes a text result; `None` becomes empty content; dicts, lists and numbers become their JSON text and are also kept as `structured_content`.

`ToolResult.text(text, details=None, structured_content=None, is_error=False, terminate=False)` creates a text result. `details` and `structured_content` are not sent to the model automatically. Content accepts `TextContent` and `ImageContent`. Input is strictly validated by default: types are not coerced and nulls are not dropped. `prepare_arguments(args)` can return new arguments, synchronously or asynchronously, which are then validated the same way; both the original and the converted arguments are kept.

Standard JSON Schema is accepted: draft-04, 06, 07, 2019-09 or 2020-12 is chosen by `$schema`, and 2020-12 is used when `$schema` is absent. Schemas generated by pydantic and the schemas of common MCP tools work as they are. As in Pi, argument validation checks `pattern` and `format`. A `format` is checked only when jsonschema has a checker for it: `email`, `date`, `ipv4` and others work out of the box, while `uri`, `date-time` and others need `jsonschema[format-nongpl]` installed. Only a `$ref` pointing inside the schema is allowed; references to the network or to files are rejected at registration. Objects and arrays in a schema may nest at most 100 levels deep (each object or array counts as one level, so one level of `properties` nesting takes two); deeper schemas raise `ConfigurationError` at registration.

### Tools from functions

```python
from pi_python import tool

@tool
def search_papers(query: str, year: int | None = None, limit: int = 10) -> list[dict]:
    """Search the paper index.

    Args:
        query: Keywords.
        year: Only papers from this year.
        limit: Maximum number of results.
    """
    ...
```

`@tool` turns a function into a `Tool`: the function name is the tool name, the first paragraph of the docstring is the description, the parameter types generate the input schema, and argument descriptions in Google, NumPy or Sphinx style go into the schema. You can also write `@tool(name=..., description=..., execution_mode=..., output_schema=...)`, or call `tool(fn)` on an existing function.

Supported parameter types: `str`, `int`, `float`, `bool`, `None`, `list`, `set`, `tuple`, `dict[str, T]`, `Literal`, `Enum`, `Optional` and other unions, `Annotated[T, "description"]`, `TypedDict`, dataclasses, `datetime`, `date`, `UUID`, `Path`, and pydantic models. Before the call, the JSON arguments are converted to the types the function asks for, such as enum members, dates, dataclasses or pydantic models. A parameter annotated as `ToolContext` receives the call context and does not appear in the schema. `*args`, `**kwargs` and types that cannot be represented as JSON raise `ConfigurationError` at registration.

Concurrency is unlimited by default: a batch of tools all run at once, as in Pi; set `RunLimits(max_concurrency=...)` when you need a cap. If any tool requires `sequential`, the whole batch runs one at a time. When running concurrently, preparation completes in call order before execution starts; end events follow the order in which post-processing finishes, and results in history follow call order.

A standalone program can call `await run_tool_call(tool, call, context, before_tool_call=..., after_tool_call=...)` to reuse the same validation and hook path. For standalone calls, the program manages lifetime, timeouts and cancellation itself; `Agent` adds batch management and `RunLimits`.

## Hooks

Every hook can be a sync or an async function. Contexts and arguments are passed as copies; the cancel token and the tool-update channel are shared control interfaces.

| Hook | Arguments and return value |
|---|---|
| `prepare_request(context, cancel)` | Returns a `TurnUpdate` or None; called before every model request |
| `prepare_next_turn(context, cancel)` | Returns a `TurnUpdate` or None; called from the second turn on |
| `transform_context(messages, cancel)` | Returns the message list used for the model request; runs before convert |
| `convert_to_llm(messages)` | Returns the message list that can be sent to the model; must handle or explicitly filter out `CustomMessage` |
| `before_tool_call(call, args, context)` | True / None allows, False blocks, or return a `ToolResult` to use without executing; if the hook raises, that call gets an error result (`hook_error`) and the run continues, as in Pi |
| `after_tool_call(call, result, context)` | Returns a full `ToolResult`, a partial `ToolResultUpdate`, or None |
| `finish_turn(context, cancel)` | Returns `"continue"`, `"end"` or None |

For the preparation and finish hooks, `context` is a `RunContext` containing `messages`, `model`, `options`, `tools`, the latest `message` and `tool_results`. `TurnUpdate(model=..., options=..., tools=..., context=..., messages=...)` affects the rest of this run without changing the Agent's defaults. `context` replaces this run's model context without rewriting committed history; `messages` are new inputs to append. Only an explicit `update_config` changes the defaults for the next run. Options are replaced field by field as whole values, not deep-merged.

When an after-hook replaces the text with `ToolResultUpdate(content=[...])`, the old `structured_content` is cleared; to keep it, supply new structured content at the same time. Successful results are checked against the output schema both before and after the after-hook. `terminate=True` suppresses the next request triggered by tools only if it holds for every final result in the batch; queued input can still trigger continuation. `finish_turn="end"` does not consume pending queues.

## Cancellation, timeouts and events

By default, `RunLimits` does not limit model requests, tool calls or tool concurrency, as in Pi; applications set `max_model_requests`, `max_tool_calls` and `max_concurrency` when they need them. The cleanup deadline defaults to 1 second. `tool_timeout` and `run_timeout` default to None and are measured on a monotonic clock. When a tool-call cap is set, it is checked before a batch starts; if the remaining allowance is not enough, none of the batch runs.

An explicit `abort` ends the run the way Pi does. `abort` can be called from any thread, for example from a GUI stop button or a watchdog thread:

- Cancellation goes through asyncio task cancellation and reaches the running model stream, tools and hooks, much like Pi's AbortSignal. Each operation ends in its own way within the cleanup deadline.
- When the model stream is cancelled, the partial content already streamed is recorded as an answer with `stop_reason="aborted"`, and `finish_turn` and `turn_end` happen as usual.
- When a tool is cancelled, it gets an ordinary error result, `Operation aborted` (`execution_status="cancelled"`). A result the tool returns itself after catching the cancellation is used as usual, and completed results are kept. After the whole batch is committed, as in Pi, one more turn begins: it sends no model request, records an aborted answer and ends.
- `prompt` returns `status="cancelled"`, and the Agent remains usable.

When the caller cancels the Python Task running `prompt`, the flow above does not apply: the library performs bounded cleanup and re-raises `asyncio.CancelledError`.

When a run fails or is cancelled outside the model answer (for example, a preparation hook raises), an `error` / `aborted` answer is appended and `turn_end` is emitted, like Pi's `handleRunFailure`. A subscriber failure is the exception: publishing stops and nothing more is appended.

A tool timeout likewise cancels that tool; the result is the error `tool_timeout`, which goes to the model, and the run continues.

When a tool cannot confirm whether an external commit succeeded, it can raise `ToolOutcomeUnknownError`. This is a guard this library adds beyond Pi: it stops the current run and refuses later prompts, continues and configuration updates, so that the application can reconcile externally and create a new instance with corrected history. Cancellation and timeouts do not trigger this guard.

Python cannot forcibly stop an uncooperative coroutine or the thread running a plain function. If a tool still has not finished by the cleanup deadline after cancellation or a timeout, its result is the error `not_stopped`, the run stops at once, and no more model requests are sent. Until the tool actually finishes, the instance stays busy: `cleanup_complete=False`, and `prompt`, `wait_for_idle` and `aclose` raise `CleanupTimeoutError`. As soon as the tool finishes, the instance becomes usable again. Applications that need to force termination should manage subprocesses themselves. A timeout or cancellation result does not mean the external operation was rolled back.

Events are `agent_start/end`, `turn_start/end`, `message_start/update/end`, `tool_execution_start/update/end`, and `config_update` for explicit configuration changes. The envelope carries `schema_version=1`, the run ID, the turn ID, a monotonic sequence number and an optional call ID. Each subscriber receives its own copy. Subscribers are awaited in order; if one fails, execution stops, and `state.diagnostics` records the failing subscriber and those not yet called. The failing subscriber is not called again recursively.

A subscriber callback must not wait for this instance to become idle, or it will end up waiting on itself. A display layer can use a bounded `EventQueue(maxsize=128, drop_text_updates=True)`: only message deltas may be dropped, the count is kept in `dropped_updates`, and final tool events always apply backpressure. On the normal path, `prompt` returning means the critical subscribers have handled the end events.

## Recovering from failures

When the model errors or is cancelled, the answer stays in history with `stop_reason="error"` or `"aborted"`, and its `error` field holds the original error text. HTTP failures also carry the response body returned by the server (at most 4000 characters, with the key used for this request replaced by `[redacted]`). The functions below are ported from upstream pi-ai. They read that answer to tell why it failed, and on 44 shared samples their verdicts match upstream:

| Function | Purpose |
|---|---|
| `is_context_overflow(message, context_window=None)` | Whether it failed because the context was too long. Given the window size, it also recognizes silent overflow (reported input larger than the window) and a length stop with zero output after truncation |
| `is_retryable_error(message)` | Whether it looks transient: overload, rate limit, 5xx, network interruption, stream ended early. Quota and billing problems do not count |
| `is_recoverable_length(message, desired_max_output)` | A length stop with less output than the desired limit, which may be caused by context pressure |
| `retry_delay(attempt, base=2.0, max_delay=60.0)` | Seconds to wait before retry number `attempt`, with exponential backoff |

`continue_run()` retries the turn when the last answer failed or was cancelled and there is no queued input. The failed answer stays in history and is skipped when the history is replayed to the model. This differs from Pi, whose application layer deletes the failed answer before continuing. [examples/recovery.py](../examples/recovery.py) shows the full approach: on overflow, use `transform_context` to replace the earliest turns with a summary and continue; on a transient error, back off with `retry_delay` and continue.

## Using it from plain scripts

`agent.prompt_sync(...)`, `agent.continue_run_sync()` and the general `run_sync(awaitable)` block and run in plain scripts, so you do not have to write `asyncio.run` yourself. They share one background event loop, so connections cached by a Provider stay usable across calls. On Ctrl+C, the current run wraps up the way `abort` does, and then `KeyboardInterrupt` is raised.

Where an event loop is already running (async programs, top-level await in Jupyter), these functions raise an error suggesting `await agent.prompt(...)` instead. Use one style per Agent: either only the blocking versions, or only await in your own event loop.

## MCP tools

```python
from pi_python.mcp import connect_stdio

async with connect_stdio("uvx", ["mcp-server-fetch"], prefix="web") as tools:
    agent = Agent(provider=..., tools=tools)
    await agent.prompt("Summarize https://example.org")
```

Requires `pip install 'pi-python-core[mcp]'` and works with the official MCP SDK 1.10 and later, including 2.x. `connect_stdio` starts a stdio MCP server and closes it when the `async with` block exits. `connect_http(url, headers=None, prefix=None, names=None)` connects to a streamable HTTP server in the same way; the legacy SSE transport is not supported, as in Pi. It follows redirects only within the server's origin, so headers are never sent to another one. It is tested with MCP SDK 1.10, 1.30 and 2.3. If you already have a `ClientSession`, wrap its tools with `await mcp_tools(session, prefix=None, names=None)`. Conversion follows Pi's MCP adapter: text and images convert directly; embedded text or image resources are unpacked; audio, resource links and binary resources become short text descriptions; a result with only structured content becomes JSON text and is also kept as `structured_content`; MCP's `isError` becomes a tool error; progress notifications become `tool_execution_update` events. After the prefix is added, tool names keep only letters, digits, `_` and `-`, up to 64 characters. A tool whose schema cannot be used is skipped with a warning, without affecting the server's other tools. A stdio server written with Python MCP SDK 2.3 cannot start on PyPy (`fcntl.F_DUPFD_CLOEXEC` is missing); this is a limitation of the SDK itself, and the client side is unaffected.

## Plugins

`pi_python.plugins` loads plugins: named bundles of tools, system prompt text, hooks, skills, prompt templates, subagents and MCP servers, in the format of Pi's packages. `async with load_plugins([...], services=..., options=...) as plugins:` loads them, and `plugins.agent(...)` builds an Agent with everything they contribute. The [plugin guide](PLUGINS.md) covers writing, distributing and combining plugins.

| Name | Purpose |
|---|---|
| `load_plugins(sources, services=None, options=None, on_error=None)` | Returns a `PluginSet`; open it with `async with`, or `with` in plain scripts |
| `PluginSet.agent(**agent_arguments)` | An Agent with the plugins' system prompt text, tools, hooks and listeners added to yours |
| `PluginSet.expand(text)` | Expands `/skill:name args` and `/template args`; other text is unchanged |
| `PluginSet.system_prompt(base)`, `.tools`, `.hooks(base)`, `.skill_tool()`, `.subagent_tool(tools=(), hooks=None, provider=None, stream_fn=None, model=None, limits=None)` | The parts, for building an Agent yourself. Without `model`, a subagent uses the calling agent's model at the time of the call; `limits` apply to each subagent run |
| `await PluginSet.check()` | Runs the plugins' self-checks; returns `CheckResult(plugin, name, passed, detail)` items |
| `PluginSet.skills`, `.prompts`, `.agents`, `.plugins`, `.diagnostics` | What was loaded: `Skill(name, description, path, plugin, disable_model_invocation)`, `PromptTemplate(name, description, content, path, plugin, argument_hint)`, `AgentDefinition`, `Plugin`, and the warning texts |
| `Plugin(name, setup=None, root=None, version=None, source="code")` | A plugin defined in code. `root=None` means no resource directory; for a `Plugin` exported through an entry point it means the exporting package's directory |
| `PluginAPI` | What a plugin's `setup(api)` receives |
| `AgentDefinition(name, description, system_prompt="", tools=None, model=None, options={}, provider=None, path=None, plugin="")` | A subagent; `path` and `plugin` record where a definition came from |
| `discover_plugins()` | Installed plugins as `InstalledPlugin(name, target, distribution, version)`, without importing them; they register in the entry point group `ENTRY_POINT_GROUP` (`"pi_python.plugins"`) |
| `PluginFailure`, `PluginWarning` | What `on_error` receives when a plugin handler fails (a report, not an exception); the warning for a resource that was skipped |

## Messages and encoding

The public message types are `SystemMessage`, `UserMessage`, `AssistantMessage`, `ToolResultMessage` and `CustomMessage`; the content blocks are `TextContent`, `ImageContent`, `ThinkingContent` and `ToolCall`. User messages and tool results can carry images; assistant messages can carry signed text, reasoning and tool calls. A `SystemMessage`'s text appends instructions, and `sections` replaces sections by name (None deletes one); tool declarations are replayed through `tools_added` / `tools_removed`.

`encode_messages` / `decode_messages` and `encode_event` / `decode_event` are pure string conversions; they do not read or write files. The message schema version is 3 and the event envelope version is 1; any other version is rejected. NaN, Infinity, functions and file handles are not accepted, and dangling calls, duplicate results and unknown versions are rejected. Importing history only validates data; it never runs the model or tools. External adapters can use `current_tools`, `current_system_message` and `current_system_prompt` to replay state.

## Standalone loop and convenience APIs

`agent_loop(prompts, AgentContext(...), AgentLoopConfig(...), cancel=None)` and `agent_loop_continue(context, config, cancel=None)` return an `AgentEventStream`, which supports `async for` and `await stream.result()`. `run_agent_loop(..., emit=None, cancel=None)` / `run_agent_loop_continue(...)` return the new messages directly and accept a sync or async event callback. They reuse the Agent's execution engine. The continue versions write history back into the context passed in; the prompt versions leave the original context unchanged, matching upstream.

The configuration includes `provider` or `stream_fn`, `model`, `options`, `hooks`, `limits`, `tool_execution`, and `get_steering_messages` / `get_follow_up_messages`. The queue callbacks are called at the same boundaries as upstream and can return message lists synchronously or asynchronously.

The event stream uses a bounded queue. If you only wait for the result, `result()` consumes the events; if you use `async for`, consume to the end before reading the result. To give up midway, call `await stream.aclose()`. Do not just wait for the producer to finish while you have paused consuming, or backpressure will block it.

`set_default_stream_fn(provider_or_function)` sets an explicit process-wide default stream; pass None to clear it. `Agent` can then omit the provider, or use `stream_fn=`. `reset()` clears the conversation and queues but keeps the replayed system prompt and tool declarations; it is refused during a run. An instance whose tool reported an unknown outcome still has to be rebuilt after reconciliation; reset does not clear that guard.

`has_queued_messages()`, `peek_queued_messages()`, `clear_steering_queue()`, `clear_follow_up_queue()` and `clear_all_queues()` correspond to upstream's convenience methods. Peeking prefers steering and previews follow-up only when steering is empty; it returns copies. `signal` returns the active `CancelToken` during a run and None when idle. `prompt(text, images=[...])` is a shortcut for sending images.

An Agent can also be constructed with `thinking_level`, `thinking_budgets`, `transport`, `session_id`, `get_api_key`, `on_payload`, `on_response` and `on_provider_stream_event`. The last four can also go in Hooks; explicit constructor arguments take precedence. Their protocols, and the Provider parameters, are described in [the connector guide](PROVIDERS.md).

## Fields of messages, tool results and events

`SystemMessage.content` accepts a string or a list of text blocks. `AssistantMessage` also contains `response_model`, `response_id`, `thinking_level`, `diagnostics`, `raw_stop_reason`, `end_turn` and `deferred`. `provider_thinking_level` is the provider's native effort; `thinking_level` is the requested level. Fields whose value is null in arbitrary user JSON are preserved intact.

`ToolResult`, `ToolResultUpdate` and the history's `ToolResultMessage` support `usage` and `nested_calls`; history also stores `details`. Nested-call records have the form `{complete: bool, calls: [{id, name, status, ...}]}`, where status is ok/error/unfinished. details, usage and nested_calls are stripped before anything is sent to the main model. Tool execution and after-hooks get assistant_message, agent_context, tool_call, args, result and is_error through `ToolContext`; `RunContext.new_messages` collects the messages added by this run. All of these contexts are copies.

Among checked events, non-terminal events carry `partial` (an independent snapshot that does not change with the next event); block boundaries carry `block`, text/reasoning ends carry `content`, tool deltas carry that block's `call_id` and `name`, and terminal events carry `message` and `reason`. Tool-argument previews may be incomplete, but execution uses the strictly parsed final arguments. The data of the Agent's `message_update` event is `delta_type`, `block_index`, `delta`, `content`, `tool_call_id` and `partial`.

OpenAI may add encrypted_content back in the final response. If the visible reasoning and the other signature fields are identical and only the missing encrypted content is added, the terminal message may carry that one extra field compared with the block-end snapshot. Text, arguments and tool identity must still match.

## Mid-session changes and context estimation

`render_system_update(message)` returns the text sent when a later system message is delivered in place: after the body, each section is written as `Updated system prompt section "name":` followed by the new content, or `Removed system prompt section "name".`. When a system message consists of several text blocks, the blocks are joined with a single newline, the same as upstream's `contentText`. How each Provider sends mid-session changes to tools, instructions and reasoning effort is described in [the connector guide](PROVIDERS.md#mid-session-changes).

`estimate_context_tokens(messages)` ports upstream's character-based estimate: the latest valid usage record plus an estimate of the messages after it, at about 1 token per 4 UTF-16 characters, with each image counted as 4800 characters. It is not a tokenizer. `clamp_max_tokens_to_context(context_window, messages, max_tokens)` uses it to reserve 4096 safety tokens, with a result of at least 1; Providers use it to shorten the output limit of known models.

`AssistantMessage.provider_thinking_level` matches upstream: it records this answer's effort only for Claude models that use effort markers, so later requests can rebuild the markers in history. Read `thinking_level` when you need the requested level.

When replaying history, Providers skip `error` / `aborted` answers following upstream's rules; reasoning from a different model is converted to plain text and its signature is dropped; a system message that sits between a tool call and its result is moved after the result. The session history itself is unchanged.
