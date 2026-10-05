# Plugins

**English** | [中文](zh/PLUGINS.md)

A plugin is a named bundle of additions to an agent: tools, instructions for the system prompt, hooks, skills, prompt templates, subagents and MCP servers. Packing them into a plugin lets you install them with pip, share them between projects, and switch them on or off as a unit. A plugin gives the agent nothing it could not do before. It assembles the tools, system prompt and hooks that you would otherwise wire into `Agent(...)` by hand, and the agent loop itself is unchanged.

The format follows Pi's packages. A Pi package corresponds to a plugin here, and the TypeScript extension code inside a Pi package corresponds to the plugin's Python `setup` function. Skills, prompt templates and subagent definitions are the same Markdown files Pi uses, so most of them can be copied over unchanged.

## Using plugins

```python
from pi_python.plugins import load_plugins
from pi_python.providers import AnthropicProvider

async with load_plugins(
    ["hpc-slurm", "./lab-plugin"],                # an installed plugin and a local directory
    services={"chooser": ask_in_terminal},        # objects the plugins may ask for
    options={"hpc-slurm": {"partition": "gpu"}},  # settings, per plugin
) as plugins:
    agent = plugins.agent(
        provider=AnthropicProvider(api_key="..."),
        model="claude-sonnet-5-5",
        system_prompt="You help run experiments on the cluster.",
    )
    result = await agent.prompt(plugins.expand("/submit run_42"))
```

In a plain script, write `with load_plugins(...) as plugins:` and call `agent.prompt_sync(...)`.

Each source is one of these:

| Source | Example | What loads |
|---|---|---|
| Name of an installed plugin | `"hpc-slurm"` | The package that registers that name (see [Distributing a plugin](#distributing-a-plugin)) |
| Path to a directory | `"./lab-plugin"` | Its `skills/`, `prompts/`, `agents/` and `mcp.json`, and its `plugin.py` if there is one |
| Path to a `.py` file | `"./extra.py"` | That file's `setup(api)`, without resource directories |
| A `Plugin` object | `Plugin("audit", setup)` | A plugin defined in your own code |

A string counts as a path when it starts with `.` or `~`, contains a slash, or ends in `.py`. Plugins load in the order given. Opening the set runs each plugin's setup and connects its MCP servers. Closing it disconnects the servers and runs the plugins' cleanup.

`plugins.agent(...)` takes the same arguments as `Agent` and returns an Agent that combines yours with the plugins':

- **System prompt:** your text, then each plugin's instructions, then the list of available skills.
- **Tools:** yours, then the plugins' tools and MCP tools, plus `read_skill` when there are skills and `subagent` when there are subagents. Two tools with the same name raise `ConfigurationError`.
- **Hooks and events:** your hooks run first, then the plugins' handlers (see [Hooks from several plugins](#hooks-from-several-plugins)). Plugin event listeners are subscribed.

`plugins.expand(text)` handles the commands a person types. `/skill:name args` inserts a skill's instructions, and `/template args` expands a prompt template. Other text passes through unchanged, so you can call it on every user input before `prompt`.

To build the Agent yourself, use the parts: `plugins.system_prompt(base)`, `plugins.tools`, `plugins.skill_tool()`, `plugins.subagent_tool(tools=..., provider=..., model=...)` and `plugins.hooks(your_hooks)`. The skill list in the system prompt tells the model to use `read_skill`, so add that tool whenever there are skills.

Problems that do not stop loading become `PluginWarning` warnings and stay in `plugins.diagnostics`. Examples are a skill without a description or an MCP server that fails to start. An exception in a plugin's setup, a service the application did not provide, or two plugins providing the same tool name raise an error and close the set.

Loading a plugin runs its code with your permissions and may start the programs its MCP servers name. Skills can also tell the model to run commands. Load only plugins you trust. Nothing is discovered or loaded automatically: a plugin runs only when your code names it. `discover_plugins()` lists the installed plugins without importing them.

## Writing a plugin

A plugin is a directory. Every part is optional; a directory with only `skills/` is a plugin too.

```text
lab-plugin/
├── plugin.py          # setup(api)
├── ops.py             # plugin.py imports it with: from .ops import count_duplicates
├── skills/
│   └── dedup-window/SKILL.md
├── prompts/
│   └── dedup.md
├── agents/
│   └── reviewer.md
└── mcp.json
```

```python
# plugin.py
from .ops import count_duplicates

__version__ = "0.1.0"


def setup(api):
    api.add_tool(count_duplicates)
    api.add_system_prompt(f"You work for {api.options.get('lab', 'the lab')}.")

    @api.on("before_tool_call")
    def keep_raw_data(call, args, context):
        # A rule enforced in code, not only stated in the prompt.
        if call.name == "delete_file" and args["path"].startswith("raw/"):
            return False

    api.add_check(lambda: count_duplicates(["a", "b", "a"]) == 1)
```

`setup` can be a plain or an `async` function. The object it receives has these members:

| Member | Purpose |
|---|---|
| `add_tool(tool_or_function)` | Adds a `Tool`, or a function that `@tool` turns into one |
| `add_system_prompt(text)` | Appends instructions to the system prompt |
| `on(hook, handler)` or `@api.on(hook)` | Handles any `Hooks` slot, such as `before_tool_call` or `transform_context` |
| `subscribe(listener)` | Receives the agent's events |
| `add_skills(path)`, `add_prompts(path)`, `add_agents(path)` | Loads resources that are not in the conventional directories |
| `add_agent(AgentDefinition(...))` | Defines a subagent in code, optionally with its own provider |
| `add_mcp_server(name, config)` | Adds an MCP server, written as in `mcp.json` |
| `service(name, default=...)` | Returns an object the application passed in `services` |
| `options` | This plugin's entry in the application's `options` |
| `add_check(function, name=None)` | Registers a self-check for `await plugins.check()` |
| `on_close(callback)` | Registers cleanup for when the set closes |
| `name`, `version`, `root` | The plugin's name, its `__version__`, and its directory |

Relative paths resolve against the plugin directory. Get anything the application must provide through `service`, for example a database connection or a way to ask a person to choose. Put tunable values, such as thresholds or allowed ranges, in `options`. The same plugin then works in different projects without edits.

`await plugins.check()` runs every self-check and returns a `CheckResult(plugin, name, passed, detail)` for each. A check passes unless it raises or returns `False`. Checks suit quick tests that the plugin's tools still give the expected answers on known inputs.

### Distributing a plugin

To install a plugin with pip, make it an ordinary Python package and register an entry point in the `pi_python.plugins` group:

```toml
# pyproject.toml
[project]
name = "pi-plugin-hpc-slurm"
version = "1.0.0"
dependencies = ["pi-python-core>=0.9"]

[project.entry-points."pi_python.plugins"]
hpc-slurm = "pi_hpc_slurm"
```

The entry point's name is the plugin's name. It can point to a module that defines `setup`, to a setup function (`"pi_hpc_slurm:setup"`), or to a `Plugin` object. The package's directory is the plugin directory, so `skills/`, `prompts/`, `agents/` and `mcp.json` go inside the package; build backends such as hatchling include them in the wheel. The plugin's version is the package's version.

## Skills

A skill is a directory with a `SKILL.md` file, as defined by the [Agent Skills specification](https://agentskills.io/specification) that Pi implements:

```markdown
---
name: dedup-window
description: How to choose a time window for merging repeated log events. Use before deduplicating event logs by time.
---

# Choosing a deduplication window

Read `references/semantics.md` for the two grouping rules.
```

The system prompt lists each skill's name, description and location, but not its instructions. When a task matches a description, the model calls `read_skill` with the skill's name to load the instructions. It can pass a relative `path` to read other files in the skill's directory, and nothing outside it; each file can be at most 256 KiB. Because the description decides when the model reaches for a skill, it should say what the skill does and when to use it. `disable-model-invocation: true` hides a skill from the model, so it loads only when a person types `/skill:name`.

Discovery follows Pi. A directory that contains `SKILL.md` is one skill and is not searched further. Otherwise, `.md` files directly in `skills/` and `SKILL.md` files in its subdirectories are skills. A skill without a description is skipped with a warning; an invalid name only gives a warning. When two plugins have skills with the same name, the first plugin's skill is used.

## Prompt templates

Each `.md` file in `prompts/` is a template named after its file. Typing `/dedup events.csv 30` expands `prompts/dedup.md`:

```markdown
---
description: Find duplicate events in a log
argument-hint: <file> [window-seconds]
---
Find duplicate events in $1 using a ${2:-60} second window.
```

| Placeholder | Becomes |
|---|---|
| `$1`, `$2`, … | One argument, or nothing if it is missing |
| `$@`, `$ARGUMENTS` | All arguments, separated by spaces |
| `${N:-default}` | Argument N, or `default` when it is missing or empty |
| `${@:N}`, `${@:N:L}` | The arguments from the Nth on, or L of them |

Arguments are split like a shell command line: spaces separate them and quotes keep words together. These rules are Pi's, and they are tested against Pi's own code.

## Subagents

A file in `agents/` defines an agent that the main agent can delegate a task to:

```markdown
---
name: reviewer
description: Checks a deduplication result by counting duplicates independently
tools: count_duplicates
model: claude-haiku-4-5
---
You check deduplication results. Report the number only.
```

The body is the subagent's system prompt. `tools`, a comma-separated list or a YAML list, names the tools it may use. Without `tools`, it gets every tool of the main agent except `subagent`. Without `model`, it uses the main agent's model at the time of the call, including a model switched by a hook. The subagent also uses the main agent's provider and all of its hooks, including the plugins' handlers. So key lookup and safety rules apply to it, and so do per-turn handlers such as `prepare_request`. This matches Pi, where each subagent is a pi process that loads the same extensions. In code, `add_agent(AgentDefinition(..., provider=..., options=...))` can give it a different provider or options.

The main agent sees a single `subagent` tool. Its description lists the available subagents, and it has Pi's three modes:

| Mode | Arguments | Result |
|---|---|---|
| Single | `agent`, `task` | The subagent's final answer |
| Parallel | `tasks: [{agent, task}, ...]` | Every answer, and how many succeeded. At most 8 tasks, 4 running at once |
| Chain | `chain: [{agent, task}, ...]` | The last step's answer. `{previous}` in a task becomes the previous step's answer; the chain stops at the first failure |

Each task runs a fresh Agent with its own history, so the main conversation sees only the answers. Each subagent run has the main agent's `RunLimits`, such as `max_model_requests`; a chain has no step limit, as in Pi, so set limits when untrusted text can reach the model. Their token usage is added to the tool result's `usage`, and each task's details go in `details`. Cancelling the main agent aborts its subagents. Pi runs each subagent as a separate process; here a subagent is an Agent in the same event loop.

## MCP servers

`mcp.json` uses the same format as Pi and other MCP clients:

```json
{
  "mcpServers": {
    "slurm": {"command": "${PYTHON}", "args": ["${PLUGIN_ROOT}/slurm_server.py"]},
    "docs": {"url": "https://example.org/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}
  }
}
```

A server with a `command` is started over stdio, with optional `args`, `env` and `cwd`. A server with a `url` is reached over streamable HTTP, with optional `headers`. In these strings, `${PLUGIN_ROOT}` is the plugin directory and `${PYTHON}` is the running Python interpreter; any other `${NAME}` is an environment variable. `enabled: false` keeps an entry without connecting it. Tools are named `mcp__<server>__<tool>`. Values taken from environment variables are masked as `***` in warnings, and a header or URL containing a control character, such as the trailing newline of a pasted token, is rejected. An HTTP server may redirect within its own origin; a redirect to another origin is not followed, so headers are never sent there.

An invalid entry in `mcp.json`, such as one that uses an unset variable, is skipped with a warning; the same mistake in `add_mcp_server` raises `ConfigurationError`. A server that fails to start or connect is also skipped with a warning, and everything else still loads. Settings this library does not support, such as `timeout`, `exposure` and `oauth`, are ignored with a warning; use `RunLimits(tool_timeout=...)` for timeouts. The SSE transport is rejected, as in Pi. MCP needs `pip install 'pi-python-core[mcp]'`. Outside plugins, `pi_python.mcp.connect_http(url, headers=...)` connects to an HTTP server the same way `connect_stdio` starts a local one.

Stdio entries also accept `"process_scope": true` on POSIX. This applies to both
`mcp.json` and `api.add_mcp_server`: the connection owns surviving process groups
and inherited nested pi-python stdio connections until it closes. The value must be
a boolean and is not accepted for HTTP. See [process ownership](MCP_INTERACTION.md)
for cancellation behavior and platform limits.

Both transports accept `call_metadata`, a JSON object sent as the MCP tool request's
`_meta`, separate from the tool schema and model arguments. Supply runtime values
through plugin options:

```python
def setup(api):
    api.add_mcp_server("sandbox", {
        "url": api.options["url"],
        "call_metadata": {"context_id": api.options["context_id"]},
    })
```

The application passes `options={"sandbox-plugin": {"url": sandbox_url, "context_id": task_id}}`
to `load_plugins`. Metadata is copied at registration and for every call. Strings stay
literal, without environment expansion. The framework does not interpret `context_id`.
Load a separate plugin set for each task context; changing options after registration
does not retarget a connection. `mcp.json` accepts the same field. Without plugins,
pass `call_metadata` to `connect_stdio`, `connect_http`, or `mcp_tools`.
The SDK still manages progress tokens and notifications. If `ClientSession.call_tool`
lacks `meta`, configuring metadata (even `{}`) raises `ConfigurationError` and aborts
loading. Omitting metadata keeps older SDKs supported.

## Strict loading and readiness

MCP services can also request host models and user forms. The host grants these with
`load_plugins(..., mcp_callbacks={(plugin_name, server_name): MCPCallbacks(...)})`.
Server JSON may list `required_capabilities`, but cannot contain executable callbacks
or grant itself capabilities. See [MCP interaction](MCP_INTERACTION.md#plugin-grants-and-readiness).

`load_plugins(..., strict=True)` rejects any loading diagnostic, including skipped
plugin resources, unsupported settings, and failed MCP connections. It raises
`ConfigurationError` and closes started connections and plugin resources. The default
remains permissive. Strict loading does not run self-checks or infer required capabilities.

Use `readiness()` to run registered self-checks and check explicit requirements:

```python
async with load_plugins(["sandbox-plugin"], options=options, strict=True) as plugins:
    status = await plugins.readiness(
        required_tools=["mcp__sandbox__execute"],
        required_skills=["sandbox-rules"],
        required_mcp_servers=["sandbox"],
        required_checks=[("sandbox-plugin", "health")],
    )
    status.require_ready()  # raises ConfigurationError with unmet requirements
    agent = plugins.agent(provider=provider)
```

`ReadinessResult` separates `loaded`, missing tools/skills/MCP servers, missing checks,
and executed check results. Checks are identified by `(plugin_name, check_name)`.
Unlike `all(c.passed for c in await plugins.check())`, a required but unregistered
check makes `ready` false. Without required checks, an empty check list is acceptable;
every registered check is still run, and any failure makes `ready` false.

Tool requirements cover plugin-contributed tools, including generated `read_skill`
and `subagent`, not extra tools later passed to `Agent`. MCP names must identify
responsive sessions; disabled or failed entries do not count. Each `readiness()` call
pings MCP sessions concurrently, bounded by `mcp_timeout` seconds per server (default 5).
Failed or timed-out sessions lose their available status and capability grants in the
snapshot; a later successful ping restores them. This is not continuous monitoring,
and a ping does not verify business functionality; register self-checks for that.
Diagnostics are included but do not themselves fail readiness in
permissive mode. Call `require_ready()` inside the context manager so failure also
closes resources. Before opening or after closing, `loaded` and `ready` are false.

## Hooks from several plugins

Several plugins can handle the same hook. Handlers run in this order: your own hook, then the plugins in load order, and within a plugin in the order it registered them. Their results combine as in Pi's extension runner:

| Hook | How the results combine |
|---|---|
| `before_tool_call` | The first handler that blocks (returns `False`) or supplies a `ToolResult` decides, and later handlers are not called |
| `after_tool_call`, `on_payload` | Chained: each handler sees the result as changed by the handlers before it |
| `transform_context`, `convert_to_llm` | Chained: each handler receives the previous handler's messages, and returning `None` keeps them |
| `prepare_request`, `prepare_next_turn` | Each handler sees the model, options, tools and context set by the handlers before it. New messages from all of them are appended afterwards, in order |
| `finish_turn` | Every handler runs, and `"end"` wins over `"continue"` |
| `get_api_key` | The first key returned is used |
| `on_response`, `on_provider_stream_event`, event listeners | Every handler runs |

When a plugin's handler raises an exception or returns the wrong type, a `PluginFailure` report goes to the `on_error` callback of `load_plugins`, which may be sync or async and logs the failure by default. That handler's contribution is skipped and the other handlers still run, so one faulty plugin does not stop the agent. The exception is `before_tool_call`, where an error fails that tool call, as in Pi. Your own hooks keep the core behavior described in the [API](API.md#hooks).

## Differences from Pi

Pi's command-line application finds and loads packages; this library has no application, so your code names the plugins to load. The other differences:

- The model reads skills with the `read_skill` tool, because this library has no general file-reading tool.
- Subagents run in the same process instead of separate ones.
- `.gitignore` files inside skill directories are not honored.
- Frontmatter is read by a small built-in parser for the part of YAML these files use. It refuses frontmatter longer than 64 KiB or nested more than 64 levels, with a warning for that file. On about 2,000 real skill, agent and command files it gave the same results as Pi's YAML library. It also accepts unquoted descriptions that contain `: `, which YAML rejects.
- MCP servers have no OAuth, tool exposure modes, per-request timeouts or `!command` values.

The item-by-item comparison is in the [coverage map](../compat/COVERAGE.md) (Chinese). A complete plugin is in [examples/plugins/lab_tools](../examples/plugins/lab_tools), and [examples/plugin_demo.py](../examples/plugin_demo.py) runs it offline.
