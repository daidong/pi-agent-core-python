# pi-python-core

**English** | [中文](README.zh-CN.md)

An embeddable Python agent core, ported from [Pi](https://github.com/earendil-works/pi) (pinned to `v1.0.0`). It does one job: send the conversation to a model, run the tools the model asks for, hand the results back, and repeat until the model answers. Tools are plain Python functions; the model can be Claude, GPT, DeepSeek, or an open model running on your own machine or cluster.

`pi-agent-core` on PyPI is a separate project that ports an older version from pi-mono (early 2026). This library follows the behavior of Pi v1.0.0, compared case by case with the upstream code's actual output, and ships its own connectors for Claude, OpenAI, DeepSeek and local models, with no model SDK required.

## Install

Python 3.11–3.14 (including free-threaded 3.14t) and PyPy 3.11. No Node.js or model SDK needed.

```bash
pip install pi-python-core      # or: uv add pi-python-core
```

You install `pi-python-core` and import `pi_python`.

| Option | What it adds |
|---|---|
| (none) | The agent core and the built-in model connectors: Claude, OpenAI, Codex, DeepSeek, and any OpenAI-compatible server (Ollama, vLLM, llama.cpp, …) |
| `[oauth]` | Verifies the identity token when you sign in with a ChatGPT account (`openai-chatgpt`); it brings in the compiled `cryptography` package. Claude subscription and Codex sign-in do not need it |
| `[providers]` | Same as `[oauth]`; keeps install commands from 0.8.1 and earlier working |
| `[mcp]` | Gives the agent the tools of MCP servers, including those a plugin declares |
| `[mcp-interactive]` | Lets MCP services request host model calls and user forms; includes sampling profiles, metering and retries. See [MCP interaction](docs/MCP_INTERACTION.md) |

Dependencies are version ranges rather than pins, so the package fits into most existing environments.

## Five-minute start

```python
from pi_python import Agent, tool
from pi_python.providers import AnthropicProvider

@tool
def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())

agent = Agent(provider=AnthropicProvider(api_key="..."), model="claude-sonnet-4-5", tools=[word_count])
result = agent.prompt_sync("How many words are in 'to be or not to be'?")
print(result.messages[-1].content[0].text)
```

`@tool` builds a tool from the function's signature and docstring; both plain and `async` functions work. In async code, use `await agent.prompt(...)`. Switching to a local model changes two lines:

```python
from pi_python.providers import OpenAICompletionsProvider

llm = OpenAICompletionsProvider(base_url="http://localhost:11434/v1", name="ollama")
agent = Agent(provider=llm, model=llm.model("qwen3:8b", context_window=40960), tools=[word_count])
```

To try it offline first: `python examples/quickstart.py`.

## What it does

| Need | How | Example |
|---|---|---|
| Write tools | Decorate a plain function with `@tool`; arguments such as pydantic models, dataclasses, enums and dates are converted automatically; hand-written or MCP-generated JSON Schema works too | [quickstart](examples/quickstart.py) |
| Connect a model | Claude and GPT through an API key or subscription sign-in; DeepSeek; any OpenAI-compatible server | [local_model](examples/local_model.py), [provider_chat](examples/provider_chat.py) |
| Use MCP tools | `async with connect_stdio(...) as tools`, or `connect_http(url)` for a remote server | [mcp_tools](examples/mcp_tools.py) |
| Let one agent call another | Wrap the sub-agent as a tool; cancellation propagates down. Plugins can also define subagents in Markdown | [subagent](examples/subagent.py) |
| Package and share capabilities | A plugin bundles tools, instructions, hooks, skills, prompt templates, subagents and MCP servers; load it by name or path with `load_plugins` and build the agent with `plugins.agent(...)` | [plugin_demo](examples/plugin_demo.py) |
| Intervene mid-run | `steer` injects guidance, `follow_up` queues the next task, `abort` cancels at any time (callable from any thread) | |
| Context full, service errors | `is_context_overflow` and `is_retryable_error` tell you why, `continue_run()` retries, `transform_context` compacts | [recovery](examples/recovery.py) |
| Save and restore conversations | `encode_messages` / `decode_messages`; your application decides where to store them | [save_restore](examples/save_restore.py) |
| Observe and audit | Subscribe to events; hooks before and after tool execution can block or rewrite tool calls | |

Apart from `provider_chat`, which needs real credentials, every example runs offline without an API key, and the tests run each one.

## Relationship to Pi

The run loop, event order, hooks, queues, and the handling of errors and cancellation all match Pi, and this is checked differentially: the same inputs go to the pinned upstream code and to this library, and the model requests, tool calls, events and final transcript are compared item by item. All cases currently agree: 25 for the core loop, 50 for model connectors, 2 for multi-turn WebSocket, and 44 error-classification samples. The plugin rules for skills, prompt templates and frontmatter are compared the same way against Pi's coding-agent code.

A few differences are deliberate, such as strict tool-argument validation without type coercion, and returning copies of state to callers. Others are additions for Python users, such as `@tool`, blocking calls, and calling `continue_run()` directly after a failure. Each one is recorded in the [coverage map](compat/COVERAGE.md) (Chinese). Features Pi keeps in its application layer (terminal UI, session file format, context compaction) are not in this core; compaction can be built with hooks, and an example shows the full approach. Plugins are the one piece taken from that layer: an optional module that reads Pi's package format and builds an ordinary Agent from it. This project uses its own version numbers and is not an official Pi release.

## Documentation

- [Concepts on one page: five ideas and one turn](docs/CONCEPTS.md)
- [Public API](docs/API.md)
- [Model connectors, subscription sign-in and local models](docs/PROVIDERS.md)
- [Plugins: skills, prompt templates, subagents and MCP servers](docs/PLUGINS.md)
- [Implementation and verification results](docs/IMPLEMENTATION.md) (Chinese)
- [Item-by-item comparison with Pi, and deliberate differences](compat/COVERAGE.md) (Chinese)
- [Rebuilding the reference and checking release candidates](reference/README.md) (Chinese)

## Development

```bash
uv sync --locked --extra oauth --extra mcp-interactive
uv run pytest -q
uv run python scripts/verify.py      # every check, including the comparison with upstream (needs Node)
```

CI is defined in `.github/workflows/ci.yml` and runs on GitHub Actions for every push: each Python version on Linux (including 3.14t and PyPy), plus macOS and Windows.
