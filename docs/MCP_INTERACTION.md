# MCP sampling and user forms

A separate MCP server can ask the host for a model response and a user answer while
handling a tool call. The host owns model credentials, authorization and UI. The server
owns its prompts, local tools, business state and output validation. No separate TCP
bridge or application RPC protocol is needed.

Install `pip install 'pi-python-core[mcp-interactive]'`. Interactive support requires
**MCP SDK >=2.3,<3** and negotiates **2025-11-25**. Ordinary MCP tools still support the
existing `[mcp]` extra and SDK >=1.10. Requesting interactions on an older SDK raises
`ConfigurationError` before starting a service. SDK 1.x does not provide the concurrent
callback dispatch and cancellation behavior this adapter needs.

This is the request-scoped, bidirectional protocol described in the
[2025 sampling specification](https://modelcontextprotocol.io/specification/2025-11-25/client/sampling).
The [2026-07-28 revision](https://modelcontextprotocol.io/specification/2026-07-28/client/sampling)
deprecates sampling and uses `InputRequiredResult` with replayed requests. That protocol
is not implemented here; a mismatched negotiation is rejected. SDK 2.3 can still speak
2025-11-25 and may emit its sampling deprecation warning.

## Run the example

```sh
uv run --extra mcp-interactive python examples/mcp_interactive.py
```

The host starts a separate stdio server. Its Agent asks the host model for two local
tool calls, executes them, asks for a final response, then elicits an output choice.
The offline Provider and **simulated** user answer need no API keys or terminal input.
Replace `DemoProvider` and `choose()` with your Provider and asynchronous UI handler.

To run the same example over HTTP, start the server in one terminal, then the host:

```sh
uv run --extra mcp-interactive python examples/mcp_interactive_server.py --http 8765
uv run --extra mcp-interactive python examples/mcp_interactive.py --url http://127.0.0.1:8765/mcp
```

## Host API

All interaction APIs are exported from `pi_python.mcp`:

```python
from pi_python.mcp import (
    MCPCallbacks, SamplingHandler, ElicitationHandler, ElicitationResponse,
    connect_stdio, connect_http,
)

async def authorize_model(context, request):
    # The host may inspect the prompt/tools, enforce budgets, or ask for approval.
    return context.server == "business"

async def ask_user(request, cancel):
    # Your UI must display request.context.server and provide review/decline/cancel.
    # Awaiting this operation must support asyncio cancellation.
    return await your_ui.show_form(request, cancel)

callbacks = MCPCallbacks(
    sampling=SamplingHandler(
        provider, model="host-selected-model", max_tokens=2048, timeout=60,
        authorize=authorize_model,
    ),
    elicitation=ElicitationHandler(ask_user, timeout=300),
    max_requests_per_call=32,
)
async with connect_stdio(command, args, callbacks=callbacks, server_name="business") as tools:
    ...  # Tools can be passed to Agent or invoked with a ToolContext.
# connect_http(url, callbacks=callbacks, server_name="business") has the same contract.
```

Supplying a handler grants that service the corresponding capability. Leaving it out
advertises no capability. `SamplingHandler(allow_tools=False)` advertises basic sampling
only. Form elicitation advertises only `elicitation.form`, never URL mode. SDK 2.3 itself
advertises both form and URL when given a callback; the adapter narrows the initialize
request through the SDK's public `send_request` method. It retains the SDK dispatcher,
progress notifications, serialization and cancellation handling.

`MCPRequestContext` carries the host-assigned `server`, optional `plugin`, reverse MCP
`request_id`, negotiated `protocol_version`, outer `run_id` and `tool_call_id`, plus any
reverse request `metadata`. The reverse request ID is **not** a parent request ID.
2025-11-25 does not supply a universal parent ID on the wire. The client therefore
allows one active outer tool call per interactive connection and associates callbacks
with that scope. Callbacks do not take the outer-call lock. Independent connections
can run concurrently. No association is encoded in application `_meta` fields;
existing `call_metadata` stays separate and continues to work.

Do not recursively call the same interactive connection from one of its callbacks:
its outer call is already waiting for that callback. Use local host operations or a
different connection. The host-assigned service label identifies configuration, not
an authenticated identity attestation or a security boundary.

## Model conversion and policy

`SamplingHandler` converts a sampling request to `ModelRequest`, calls `Provider.stream`,
validates its terminal event and returns a standard MCP sampling result. It never closes
the supplied Provider. Reuse **one SamplingHandler per shared Provider**; its semaphore
defaults to `max_concurrency=1`. Raise that limit only for a reentrant Provider.
Providers also used directly by the host need a host-wide concurrency policy; this
semaphore cannot serialize calls made outside the handler.

| Input or behavior | Support |
|---|---|
| Multi-turn text and system prompts | Supported; server Provider replays system sections using pi-python's normal system-prompt helper |
| Tool declarations | Names, descriptions and input schemas; extra declaration fields are rejected |
| Multiple tool calls and results | IDs and names retained; duplicate, missing, mixed or unmatched results rejected |
| Tool selection | `auto`, `required`, `none`; use `tool_choice_format="anthropic"` for Anthropic's `any` spelling, otherwise the OpenAI-style spelling |
| Images in user messages or tool results | Host opt-in `allow_images=True`; the host Provider/model must support them |
| Reasoning blocks and provider signatures | Preserved by the host-state extension below; unsupported in basic sampling |
| Audio, assistant images, custom messages | Rejected; not silently dropped |
| Model selection | Fixed by the host's `model` (string or `ModelInfo`); server preferences are advisory and do not override it |
| Token budget | Lesser of requested `maxTokens`, host `max_tokens`, and host `options["max_tokens"]` if set |
| Temperature | Server parameter requires `allow_temperature=True`; host options take precedence |
| Provider-specific options | Host `options` only; server `metadata`, stop sequences and unknown parameters are rejected |
| Tool choice in host options | A conflicting server choice is denied; use `authorize` for finer policy |
| Host context inclusion and task-augmented requests | Unsupported and not advertised |

The authorization hook receives a detached request and must return exactly `True`.
It can reject prompts/tools or apply application budgets. It is not a mutation hook.
Timeout covers authorization, waiting for the Provider gate, and generation. The
per-call callback budget counts sampling and elicitation together. Server Agents can
also use ordinary `RunLimits`. Reverse generations do not automatically consume the
outer Agent's model-request budget or contribute token usage: standard sampling replies
do not carry pi-python usage accounting. Use `SamplingHandler(observe=...)` below for charging.

## Server API

```python
from pi_python import Agent, RunLimits
from pi_python.mcp import SamplingProvider

@server.tool()
async def business(label: str, ctx: Context) -> str:
    async with SamplingProvider(ctx.request_context, timeout=120) as provider:
        agent = Agent(provider=provider, tools=local_tools,
                      limits=RunLimits(max_model_requests=4))
        result = await agent.prompt(label)
        # Check result.status and validate the business result before using it.
    answer = await ctx.session.elicit_form(
        "Choose an output", form_schema,
        related_request_id=ctx.request_context.request_id,
    )
    ...
```

Pass the current SDK request context explicitly. No process-global session or current
request is used. Do not retain the provider beyond that request or launch detached
background work with it. Its async context manager revokes it and joins outstanding
requests; it never closes the SDK session. `related_request_id` uses the SDK's public
API, including HTTP response-stream routing. Notification contexts and modern protocol
contexts are rejected.

`SamplingProvider.stream` produces one `ModelEvent.done` after the complete reply. It
does not simulate token streaming. The host chooses the model; the server's
`ModelRequest.model` does not grant access to that model. Only `max_tokens`, `temperature`
and canonical `tool_choice` options are standard sampling parameters. The negotiated
`sampling_profile` selector is described below. API keys and provider hooks cannot
be transported. Tool-result `details`, usage and nested-call accounting are local
bookkeeping, not model content, and are not sent.

## Forms and outcomes

The UI receives `ElicitationRequest(context, message, schema)` and a `CancelToken`.
Return `ElicitationResponse("accept", content)`, `ElicitationResponse("decline")`, or
`ElicitationResponse("cancel")`. Decline means an explicit refusal; cancel means the
user dismissed the interaction. Neither carries content.

The supported schema is a flat JSON object with text, boolean, number/integer and
single-choice string fields (`enum` or titled `oneOf` entries). String length,
email/URI/date/date-time format, numeric bounds and required fields are checked. Arrays,
nested objects and schema references are rejected. Accepted content must pass the
schema, may not contain unrequested fields, and is never filled from defaults.
The `[mcp-interactive]` extra includes the optional JSON Schema format validators;
without a validator, a requested format is rejected rather than ignored.
Server-supplied `pattern` constraints are rejected before schema compilation or UI
invocation. Python's regex engine can block the event loop, and an asyncio timeout
cannot interrupt it. Use length limits, supported formats or enumerated choices instead.

| Outcome | Protocol result |
|---|---|
| User accepts / declines / cancels | `ElicitResult.action = accept / decline / cancel` |
| Host policy refuses | Error `-1` |
| Callback timeout | Error `-32001`, never acceptance |
| Request budget exceeded | Error `-32000` |
| Invalid request or answer | Error `-32602` |
| Final Provider failure | Error `-32002` with sanitized category/retry data; see below |
| UI or other handler failure | Sanitized error `-32603` |
| Outer request cancelled | Single-request cancellation; where a reply is still possible, error `-32800` |
| Transport closes | SDK connection error; callbacks are cancelled and joined |

Form mode must not collect passwords, API keys, access tokens or payment credentials,
as required by the [elicitation specification](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation).
The adapter rejects obvious credential field names and unsupported sensitive formats.
This is not semantic detection: the host UI/policy must also refuse sensitive questions,
show which service is asking, and let the user review, edit, decline or cancel.
URL-based sensitive interactions are outside this implementation.

`MCPCallbacks(on_event=...)` optionally receives `MCPCallbackEvent(context, kind, status)`.
Events contain no prompt or answer text. Metadata is excluded from the context's repr.
Provider/UI exception bodies are not sent to the server or logged by this adapter.
Audit handlers should be quick; failures are isolated and cleanup waits at most one
second for them. Host logging remains the host's responsibility.

## Plugin grants and readiness

Executable handlers are supplied by the host, keyed by `(plugin_name, server_name)`:

```python
async with load_plugins(
    ["business-plugin"], strict=True,
    mcp_callbacks={("business-plugin", "business"): callbacks},
) as plugins:
    status = await plugins.readiness(
        required_mcp_capabilities={"business": ["sampling.tools", "elicitation.form"]},
    )
    status.require_ready()
```

A server configuration, in `mcp.json` or `api.add_mcp_server`, can include
`"required_capabilities": ["sampling.tools", "elicitation.form"]`. This is a requirement,
not a grant. Missing host grants fail loading even in permissive mode. Unknown runtime
grant keys fail clearly. No handler object is accepted through JSON configuration.
`readiness(mcp_timeout=5)` pings sessions concurrently before checking requirements.
A failed or timed-out ping removes the server and its grants from that snapshot; a
later successful check can restore a responsive session. `plugins.mcp_capabilities`
reflects connection setup or the latest readiness check, not continuous monitoring;
`ReadinessResult.missing_mcp_capabilities` names unmet `(server, capability)` pairs.
The existing metadata snapshots, strict loading and readiness checks remain available.

## Cancellation and ownership

Cancelling an outer tool task or its `ToolContext.cancel` stops that call's reverse
work. SDK cancellation notifications stop the server request; cancelling one call does
not close the shared session. Agent tool/run timeouts use this same cancellation path.
Closing a connection or losing transport cancels callback tasks. Owned child tasks are
joined before scope exit. Host asynchronous callbacks/Providers must cooperate with
cancellation; Python cannot forcibly terminate code that blocks the event loop or
suppresses cancellation indefinitely. The adapter closes iterators it consumes, but
never the host Provider, UI, or other borrowed services.

For stdio, the SDK owns the launched process. SDK 2.3 closes stdin, waits for graceful
exit, then escalates if necessary. POSIX escalation signals the process group; children
that leave it with `setsid` escape. If the parent exits gracefully, surviving children
are not guaranteed to be killed on POSIX unless the optional process scope is enabled.
On Windows the SDK uses Job Objects, subject to OS permissions and job restrictions.
HTTP connections never own or terminate the remote server process.

On POSIX, use `connect_stdio(command, args, process_scope=True)` to own a tree of
local MCP connections. The SDK still manages the direct server. An exec launcher
registers its process group before running server code, and nested pi-python
`connect_stdio` connections automatically inherit ownership, including when their
SDK starts a new session. Closing a nested connection cleans only its subtree.
Closing the outer connection also cleans abandoned nested groups after a server
crash. The same cleanup runs on normal exit, exceptions, failed handshakes, and
cancellation. Surviving groups receive SIGTERM, then SIGKILL after a shared grace
period of up to one second; repeated caller cancellation does not abandon this step.
One malformed record or signal failure does not skip the remaining groups. Cleanup
failures are raised, or attached as notes to an existing exception.

The default is `process_scope=False`; outside an inherited scope this leaves SDK
behavior unchanged. Inside a scoped server, the default still joins its parent's
scope. Private temporary files and `PI_MCP_PROCESS_SCOPE` carry ownership, not tool
data. Do not set or modify that internal environment variable or its files. There is
no application registration API or application output directory requirement.
The launcher uses the host Python interpreter and executes the requested command
with its original arguments, working directory, and configured environment.

This is cooperative process-group cleanup, not arbitrary descendant discovery.
Children that independently detach without using pi-python's stdio adapter are
outside the scope. The owner must remain alive to run cleanup; SIGKILL of the host
or machine failure cannot run Python finalizers. OS permissions can prevent signals.
Explicitly enabling the option on non-POSIX systems raises `ConfigurationError`;
the default Windows SDK path is unchanged. POSIX hosts advertise
`"stdio-process-scope-v1"` in `MCP_FEATURES`. Tests cover normal and forced shutdown,
nested server crashes, cancellation, startup timeout, and sibling isolation.

Process separation and MCP are communication mechanisms, **not a security sandbox**.
Use in-process plugins for trusted code that needs direct Python objects and shared
state. Use MCP for independently deployed tools, language/runtime separation, and
request-scoped model/user interaction where this explicit protocol contract fits.

## Real Providers, profiles, accounting, and retries

The installed build exposes `pi_python.mcp.MCP_FEATURES`. Require the markers
`sampling-host-state-v1`, `sampling-profiles-v1`, `sampling-metering-v1`, and
`sampling-retry-v1`. These features ship in `0.10.0`; feature checks also distinguish
earlier development builds that still carried a `0.9.0` version.
Install this checkout with `pip install '.[mcp-interactive]'`, or build an installable
wheel with `uv build --wheel`. The SDK and protocol requirements above still apply.

### Retained vendor state

`SamplingHandler(retain_state=True)` (the default) advertises the versioned experimental
capability `io.pi-python/sampling-v1`. `SamplingProvider` uses it only when advertised.
Use `SamplingProvider(context, require_host_state=True)` to fail on entry when it is absent.
Plugins can require/check `sampling.host_state` and `sampling.profiles` through the
existing capability and readiness APIs. These are pi-python features, not standard MCP capabilities.

The host retains the original `AssistantMessage`, including provider/API/model identity,
text signatures, signed tool calls, reasoning blocks, response IDs, and usage. Only visible
text/tool calls and a random reference go to the server. A subsequent request returns the
reference; the host checks the visible content and restores the original message before
Provider replay. OpenAI message IDs/phases, `call_id|item_id`, encrypted reasoning, and
Anthropic thinking signatures therefore survive multi-turn tool execution. A response
containing only reasoning projects to empty text; the reasoning remains on the host.

The negotiated request `_meta["io.pi-python/sampling-v1"]` contains `conversation`,
`profile`, and `history` (references in assistant-message order). The corresponding result
metadata is `{"ref": "..."}`. The server's `AssistantMessage.response_id` holds this
opaque reference, **not the vendor response ID**. This is a state-replay extension, not
parent-request association: the SDK's `related_request_id` still performs that job.
Business code does not need to construct these fields.

State belongs to one outer tool call, and is cleared on completion, cancellation,
disconnection, or close, after callbacks have joined. References cannot cross connections,
outer calls, SamplingProvider instances, or profiles. Modified retained assistant content
is rejected. Keep an Agent's turns inside the same SamplingProvider scope. Do not discard
`response_id`, import signed history from a different Provider, or persist these handles
for use in another outer call. Tokens and callback-count limits bound normal retained output;
host authorization can impose additional application quotas.

This is not full direct-Provider equivalence: reasoning stays invisible to server business
code, usage stays on the host, and process restart cannot recover state. Audio, assistant
images, namespaced/native server tools, and deferred outputs remain unsupported. Provider transport must be SSE; explicit WebSocket,
cached WebSocket, and automatic transport modes are rejected before model access. Delivery
is one completed event, not token streaming. Local HTTP/SSE fixtures exercise real OpenAI
Responses, Anthropic Messages, and compatible Chat Completions parsers, including reasoning
plus tool calls. They do not certify every model or gateway's private format. Without the
extension, explicit reasoning settings and known Responses Providers fail before generation;
other unrepresentable responses fail explicitly rather than losing signatures.

### Host-approved request profiles

```python
from pi_python.mcp import SamplingHandler, SamplingProfile, SamplingRetryPolicy
from pi_python.providers import HTTPTransport, OpenAIProvider

provider = OpenAIProvider(transport=HTTPTransport(max_retries=0))

async def prepare(context, profile, request):
    request.api_key = await host_credentials.for_task(context.run_id)
    request.on_payload = host_on_payload
    request.on_response = host_on_response
    request.on_provider_stream_event = host_on_stream_event

async def observe(event):
    await accounting.record(event.context.run_id, event.context.tool_call_id,
                            event.context.request_id, event.profile,
                            event.attempt, event.usage, event.status)

handler = SamplingHandler(
    provider, model=model_info, options={"parallel_tool_calls": False},
    profiles={
        "json": SamplingProfile(model_info, {"text": {"format": {"type": "json_object"}}}),
        "text": SamplingProfile(model_info),
    },
    prepare=prepare, observe=observe,
    retry=SamplingRetryPolicy(max_attempts=3, initial_delay=0.5),
)
```

`default` is configured by the handler constructor and cannot be replaced in `profiles`.
All profiles share the handler's Provider concurrency gate. A server selects a permitted
name with `SamplingProvider(profile="json")` or per-call
`ModelRequest.options["sampling_profile"] = "json"`. Unknown names are denied.
Independent tool-agent, JSON-helper, and text requests can alternate within the same task;
do not mix stateful histories from different profiles. The runnable
`examples/mcp_interactive.py` demonstrates all three and a form, offline.

Use each Provider's existing parameter vocabulary. Responses uses `text.format`;
Chat Completions uses `sampling_params={"response_format": ...}` and puts
`parallel_tool_calls` in `sampling_params` too. For Anthropic, use
`tool_choice_format="anthropic"` and, for example,
`options={"tool_choice": {"type": "auto", "disable_parallel_tool_use": True}}`.
The conversion retains that parallelism flag. Requests without tools remove tool choice
and parallel-tool parameters, including those inside `sampling_params`. Business code
still validates the actual JSON output against its requirements.

`prepare(context, profile, request)` is trusted host code that mutates a request before
`authorize` sees its independent copy. It can inject request-level credentials and Provider
callbacks, or apply additional host policy. The server never sends credentials, executable
hooks, or arbitrary Provider options. Do not install the same hook again in a wrapper.

### Accounting and failure ownership

`observe(SamplingObservation)` runs once per completed Provider stream attempt, before
retry or conversion. It exposes outer task/tool IDs, reverse-request ID, profile, attempt,
status, original vendor response ID, and a copy of actual `usage`, including cache reads
and writes. Missing usage is `None`, not zero. When absent on the wire, the three Provider parsers add
`{"type": "usage_unavailable"}` to `diagnostics`. Their existing normalized usage fields
remain compatible for direct callers; the observer honors this marker rather than counting
default zeros as measurements. Standard sampling replies still contain no usage: charge
at the root host's actual Provider, not at a nested SamplingProvider relay.
For Chat Completions, empty or incomplete usage objects also mean unknown. Both
`prompt_tokens` and `completion_tokens` must be finite, nonnegative numbers; explicit
zero counts remain valid measurements. Empty trailing chunks do not erase a complete
measurement, and valid choice-level counters are accepted when top-level usage is empty.

Observers should promptly persist the record. Observer failure ends the request without
retrying an already completed model response. Durable delivery across process crashes is
the host's responsibility. Use run/tool/reverse-request/attempt together for deduplication;
a vendor ID alone is insufficient. Partial usage on a broken or unfinished stream may
require the host's raw Provider event callback; the framework never guesses it. Borrowed
Providers are never closed by the handler.

The optional `SamplingRetryPolicy` defaults to one attempt. It retries only structured
HTTP 429/500/502/503/504 failures, never authentication/request/unknown errors or cancellation.
It does not parse raw exception strings. Exponential local backoff is capped by `max_delay`;
a longer server `Retry-After` is honored, while the total callback timeout remains in force.
Cancellation interrupts backoff. Only the current model call is repeated; completed business
tools are not replayed.

`HTTPTransport` already defaults to bounded HTTP retries. Either keep those and leave the
handler at one attempt, or set `HTTPTransport(max_retries=0)` as above and let the handler
own retries. Do not multiply retry budgets across layers. Observation `attempt` counts
Provider stream invocations; rejected internal transport attempts can be observed through
`on_response` and normally have no model usage. Do not automatically retry the outer tool.

Final model failure is MCP error `-32002` with safe `data`: `category` (`rate_limit`, `server`,
`authentication`, `request`, or `provider`), `retryable`, `retryAfter`, `attempts`, and
`retryOwner="host"`. `retryable` describes the cause, not permission to repeat business
side effects. Direct handler calls raise `SamplingFailure`; SDK callers can inspect
`MCPError.data`. Timeout, denial, and cancellation retain their distinct error codes.
No raw Provider error body, credential, or prompt is logged by default.
