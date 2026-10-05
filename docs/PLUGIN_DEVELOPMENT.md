# Plugin development workflow

**English** | [中文](zh/PLUGIN_DEVELOPMENT.md)

This is the standard workflow for a **pi-python library plugin**. It is an ordinary
Python package loaded by `pi_python.plugins`, not a Codex/ChatGPT plugin. Start with
one tool, make directory loading pass, then build a wheel and verify entry-point
loading outside the source directory. The [integration guide](CODING_AGENTS.md)
explains the host application; [Plugins](PLUGINS.md) documents the full API.

## 1. Define what the plugin owns

The host application owns providers, credentials, conversations and policy. A plugin
registers tools, instructions, hooks and optional resources through `setup(api)`.
Pass shared application objects with `services={...}` and per-plugin configuration
with `options={plugin_name: {...}}`. Do not create an agent or make model requests
at module import time.

The example below owns a `count_words` tool. It counts whitespace-separated words:
`one two three` produces `{"words": 3}`. The model's answer is checked separately
from that tool result. No network, filesystem mutation or credentials are needed.

## 2. Create the minimal package

Create this tree in a new working directory. The filenames below correspond to the
complete snippets in this guide. `skills/` and `prompts/` are optional extensions;
include them here because the demo verifies their discovery too.

```text
plugin-workspace/
├── pyproject.toml
├── demo.py
└── src/
    └── text_tools/
        ├── __init__.py
        ├── plugin.py
        ├── ops.py
        ├── pi-plugin.json
        ├── prompts/count.md
        └── skills/word-count/SKILL.md
```

### `src/text_tools/ops.py`

```python
from pi_python import tool


@tool
def count_words(text: str) -> dict[str, int]:
    """Count whitespace-separated words in text.

    Args:
        text: The text to count; repeated whitespace is one separator.
    """
    return {"words": len(text.split())}
```

### `src/text_tools/plugin.py`

```python
from pi_python.plugins import PluginAPI
from .ops import count_words

__version__ = "0.1.0"


def setup(api: PluginAPI) -> None:
    api.add_tool(count_words)
    label = api.options.get("label", "the writing team")
    api.add_system_prompt(f"Help {label}. Use count_words for whitespace word counts.")
    api.add_check(
        lambda: count_words("one  two\nthree") == {"words": 3},
        name="word_count",
    )
```

`setup` can be sync or async. Directory loading imports `plugin.py` as a package
member, so `.ops` works. Pass the directory, not `plugin.py`, when using these relative
imports. The check is registered here; it runs when the host calls `check()` or
`readiness()`, not during setup.

### `src/text_tools/__init__.py`

```python
from .plugin import setup

__all__ = ["setup"]
```

This exports setup for the installed package's entry point. Directory loading still
uses `plugin.py` directly.

### `src/text_tools/pi-plugin.json`

```json
{
  "requires": [
    "plugin-requires-v1",
    "directory-relative-imports",
    "named-readiness-checks"
  ]
}
```

Only `requires` is accepted. List the framework features the plugin depends on;
do not put package metadata, credentials or executable callbacks here. Directory
loading checks this file before importing `plugin.py`. Installed entry points must
be imported to resolve them, so their checks are before setup, not before import.
Requirements describe available APIs, not MCP grants or installed extras.

### `src/text_tools/prompts/count.md`

```markdown
---
description: Count whitespace-separated words
argument-hint: <text>
---
Count the words in this text with count_words: $ARGUMENTS
```

### `src/text_tools/skills/word-count/SKILL.md`

```markdown
---
name: word-count
description: Count whitespace-separated words with count_words when the user asks for a word count.
---
Use count_words on the supplied text and report its words field.
This rule counts whitespace-separated tokens, not linguistic words in every language.
```

Skills are listed in the system prompt and read on demand with `read_skill`.
Prompt templates require `plugins.expand(...)`; `Agent.prompt` does not expand slash
commands by itself. Instructions describe behavior; enforce business restrictions
in tool code or hooks when they must be guaranteed.

## 3. Load strictly and verify an actual tool call

Save this as `demo.py` at the project root. Run from `plugin-workspace` with a Python
environment containing this library's 0.10.0 source line. Before that release is
available, install the library checkout as described in the [integration guide](CODING_AGENTS.md).

```python
import asyncio
import sys

from pi_python import AssistantMessage, RunLimits, ScriptedProvider, ToolCall
from pi_python.plugins import load_plugins


async def main(source: str) -> None:
    provider = ScriptedProvider([
        AssistantMessage([
            ToolCall("count-1", "count_words", {"text": "one two three"}),
        ], stop_reason="tool_use"),
        AssistantMessage.text("3 words."),
    ])
    async with load_plugins(
        [source],
        options={"text_tools": {"label": "the docs team"}},
        strict=True,
    ) as plugins:
        status = await plugins.readiness(
            required_tools=["count_words", "read_skill"],
            required_skills=["word-count"],
            required_checks=[("text_tools", "word_count")],
        )
        status.require_ready()
        prompt = plugins.expand("/count one two three")
        assert prompt == "Count the words in this text with count_words: one two three"
        async with plugins.agent(
            provider=provider,
            limits=RunLimits(max_model_requests=4, max_tool_calls=4),
        ) as agent:
            result = await agent.prompt(prompt)
            assert result.status == "completed", result.errors
            outcome = result.tool_outcomes[0]
            assert outcome.call.name == "count_words"
            assert outcome.execution_status == "succeeded"
            assert outcome.result.structured_content == {"words": 3}
            print("plugin ready; count_words returned 3")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "./src/text_tools"))
```

```bash
python demo.py ./src/text_tools
```

Expected output: `plugin ready; count_words returned 3`.

`strict=True` rejects loading diagnostics; it does not run checks. `readiness()` runs
checks and verifies explicit requirements, and `require_ready()` raises on failure.
Keep the agent inside the plugin context so its tools cannot outlive their resources.
The local directory name and installed entry-point name are both `text_tools` here;
those are the keys for `options` and named checks, not the distribution name.

## 4. Package and verify the installed plugin

### `pyproject.toml`

```toml
[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "pi-plugin-text-tools"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pi-python-core>=0.10.0,<0.11"]

[project.entry-points."pi_python.plugins"]
text_tools = "text_tools"

[tool.hatch.build.targets.wheel]
packages = ["src/text_tools"]
```

The entry point points to the package exporting `setup`. Keeping Markdown and JSON
resources inside that package allows them to be included in its wheel. Match the
dependency range to the library versions you have tested. A manifest alone cannot
make an older framework understand new APIs.

From the plugin project root:

```bash
uv build
uv venv .venv-installed
uv pip install --python .venv-installed/bin/python dist/pi_plugin_text_tools-0.1.0-py3-none-any.whl
```

On Windows, use `.venv-installed/Scripts/python.exe` in place of
`.venv-installed/bin/python`. If 0.10.0 is not on your package index, first install
the library checkout (or its built wheel) into this environment, then install the
plugin wheel. Do not remove the dependency requirement to hide a version mismatch.

Copy `demo.py` to an empty directory outside the plugin source tree. From there, run
the **absolute path** to the installed environment's interpreter:

```bash
/absolute/path/to/plugin-workspace/.venv-installed/bin/python demo.py text_tools
```

Expect the same output. This verifies entry-point discovery, packaged resources,
readiness and tool execution without relying on a source checkout or editable plugin
install. Do not claim packaging is verified merely because directory loading works.

## 5. Add capabilities only as needed

| Capability | Implementation pattern |
|---|---|
| Shared client or host UI | `client = api.service("client")`; the host supplies it with `services={"client": client}`. Agree on ownership; do not close a borrowed client by default. |
| Plugin-owned resource | Create it in setup, immediately register `api.on_close(resource.aclose)`. Cleanup runs in reverse registration order, including when loading fails. |
| Async work tied to plugin lifetime | Create `scope = api.task_scope()` after registering resources it uses. Await `scope.run(client.fetch(...), cancel=context.cancel)` in an async tool with a `ToolContext` parameter. Declare `task-scope-v1` if using it. |
| Worker thread needs a loop-bound connection | Host creates a `LoopPortal` on the connection's loop and passes it as a service. The synchronous tool uses `portal.call(async_function, ...)`. See [API](API.md#owned-tasks-and-calls-from-worker-threads). |
| A hook | Register `@api.on("before_tool_call")`; return `False` to block. Use [documented hook signatures](API.md#hooks), and test the blocked path. |
| Subagent | Add `agents/reviewer.md` with `name`, `description`, optional `tools`/`model`, and a system-prompt body. See the [lab_tools example](../examples/plugins/lab_tools). |
| MCP server | Add `mcp.json` or call `api.add_mcp_server`. Install `[mcp]`; specify required servers/tools in readiness. Use the [interactive workflow](MCP_INTERACTION.md) for host model/form callbacks. |

`api.task_scope()` owns calls submitted through it; it does not adopt arbitrary tasks
started with `asyncio.create_task`. Work must cooperate with cancellation. Register
and close resources at their actual ownership boundary.

## 6. Completion criteria and common failures

Before delivering a plugin, verify directory and installed-wheel loading, exact tool
outputs, options/services, named readiness checks, and relevant invalid-input and
cleanup paths. Missing required resources should fail clearly. Test a real model
separately; the scripted demo verifies integration, not model quality.

| Symptom | Check |
|---|---|
| `No installed plugin is named ...` | Use `./src/text_tools` for a directory or install a wheel with the `pi_python.plugins` entry point. A bare name means an installed plugin. |
| Relative import fails | Load the directory rather than its `plugin.py` file. |
| Options or required checks are missing | Use the resolved plugin name: directory basename or entry-point key. |
| A skill is absent | Include its `SKILL.md` and nonempty frontmatter description in the wheel; run strict loading and require that skill. |
| `/count` reaches the model unchanged | Call `plugins.expand(...)` first. |
| New APIs are missing | Check the installed library version and declared features; install the intended checkout/build. |
| MCP setup appears to succeed but tools are absent | Use strict loading and require the server/tool in readiness; default loading can warn and skip unavailable servers. |

For a runnable multi-resource example already in this repository, run
`uv run python examples/plugin_demo.py`. It also demonstrates skill reading and a
reviewer subagent. Its source is [lab_tools](../examples/plugins/lab_tools).
