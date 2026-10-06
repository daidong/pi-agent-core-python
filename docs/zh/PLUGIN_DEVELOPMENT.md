# 插件开发标准流程

[English](../PLUGIN_DEVELOPMENT.md) | **中文**

本文指导开发 **pi-python library 的插件**。它是由 `pi_python.plugins` 加载的普通 Python 包，不是 Codex 或 ChatGPT 插件。先实现一个工具，跑通目录加载，再构建 wheel，并在源码目录之外验证入口点加载。宿主应用的接入见 [coding agent 指南](CODING_AGENTS.md)，完整接口见 [Plugins](PLUGINS.md)。

## 1. 明确插件负责什么

宿主应用负责 Provider、凭据、对话和策略。插件通过 `setup(api)` 注册工具、说明、钩子和可选资源。共享应用对象放在 `services={...}`，每个插件的配置放在 `options={插件名: {...}}`。不要在模块导入时创建 Agent 或发起模型请求。

下面的插件提供 `count_words` 工具，按空白分隔计数：输入 `one two three`，输出 `{"words": 3}`。验证时检查工具的真实结果，不只检查模型的回答。不需要网络、文件写入或凭据。

## 2. 创建最小包

在一个新的工作目录中创建以下结构。下面每段完整代码都对应一个文件。`skills/` 和 `prompts/` 是可选扩展，这里一并加入，让示例也能验证资源发现。

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

`setup` 可以是同步或异步函数。目录加载把 `plugin.py` 作为包成员导入，因此 `.ops` 相对导入可以工作。使用这种结构时传入目录，不要单独加载 `plugin.py`。此处只注册自检；宿主调用 `check()` 或 `readiness()` 时才会运行自检。

### `src/text_tools/__init__.py`

```python
from .plugin import setup

__all__ = ["setup"]
```

它为安装包入口点导出 `setup`。目录加载仍然直接使用 `plugin.py`。

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

清单只接受 `requires`。列出插件依赖的框架能力，不要写入包元数据、凭据或可执行回调。目录加载会在导入 `plugin.py` 前检查它。安装包入口点必须先导入才能解析，所以对应检查发生在 setup 前，而非导入前。这些声明描述可用 API，不代表 MCP 授权，也不验证 extra 是否已安装。

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

技能先列在系统提示中，模型需要时调用 `read_skill` 读取。提示模板要显式经过 `plugins.expand(...)`，`Agent.prompt` 不会自行展开斜杠命令。文字说明描述行为；必须保证的业务限制应在工具代码或钩子中执行。

## 3. 严格加载，验证真实工具调用

将下面代码保存为项目根目录的 `demo.py`。在 `plugin-workspace` 下，用已安装本库 0.10.0 源码系列的 Python 环境执行。如果该版本尚未发布，先按[接入指南](CODING_AGENTS.md)安装本库 checkout。

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

预期输出：`plugin ready; count_words returned 3`。

`strict=True` 拒绝加载阶段的诊断，但不运行自检。`readiness()` 运行自检并核对显式要求，`require_ready()` 在不满足要求时抛错。Agent 的生命周期放在插件上下文内部，避免工具使用已经关闭的资源。

本例的本地目录名和安装入口点名都为 `text_tools`；`options` 和具名检查使用这个名字，而不是 Python distribution 名。

## 4. 打包并验证安装后的插件

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

入口点指向导出 `setup` 的包。Markdown 和 JSON 资源放在包目录中，才能一起进入 wheel。依赖范围应与实际测试过的 library 版本匹配；只写清单不能让旧框架理解新 API。

从插件项目根目录执行：

```bash
uv build
uv venv .venv-installed
uv pip install --python .venv-installed/bin/python dist/pi_plugin_text_tools-0.1.0-py3-none-any.whl
```

Windows 下把 `.venv-installed/bin/python` 换成 `.venv-installed/Scripts/python.exe`。如果包索引中还没有 0.10.0，先把 library checkout 或它构建出的 wheel 安装到这个环境，再安装插件 wheel；不要通过删除依赖要求来掩盖版本不匹配。

把 `demo.py` 复制到插件源码树外的空目录。在那个目录中，使用已安装环境解释器的**绝对路径**运行：

```bash
/absolute/path/to/plugin-workspace/.venv-installed/bin/python demo.py text_tools
```

预期输出相同。这样可以验证入口点发现、包内资源、就绪状态和工具执行，而不依赖源码目录或插件的 editable install。目录加载成功不等于安装包验证成功。

## 5. 按需要增加能力

| 能力 | 实现方式 |
|---|---|
| 共享客户端或宿主 UI | `client = api.service("client")`；宿主通过 `services={"client": client}` 提供。先约定归属，不要默认关闭借用的客户端。 |
| 插件自己创建的资源 | 在 setup 中创建后立即注册 `api.on_close(resource.aclose)`。清理按注册逆序执行，加载失败时也会执行。 |
| 与插件生命周期绑定的异步工作 | 先注册所用资源的清理，再创建 `scope = api.task_scope()`。带 `ToolContext` 参数的异步工具中，用 `await scope.run(client.fetch(...), cancel=context.cancel)` 调用。使用时声明 `task-scope-v1`。 |
| 工作线程访问绑定事件循环的连接 | 宿主在连接所属循环创建 `LoopPortal`，通过 service 传入。同步工具用 `portal.call(async_function, ...)`。见 [API](API.md)。 |
| 钩子 | 注册 `@api.on("before_tool_call")`，返回 `False` 阻止调用。按[文档签名](API.md)实现，并验证被阻止的路径。 |
| 子 Agent | 添加 `agents/reviewer.md`，包含 `name`、`description`、可选 `tools`/`model` 和系统提示正文。见 [lab_tools 示例](../../examples/plugins/lab_tools)。 |
| MCP 服务 | 添加 `mcp.json` 或调用 `api.add_mcp_server`。安装 `[mcp]`，并在 readiness 中要求对应服务和工具。宿主模型或表单回调按 [MCP 交互流程](MCP_INTERACTION.md)配置。 |

`api.task_scope()` 只管理通过它提交的调用，不会接管任意 `asyncio.create_task` 创建的任务。工作本身必须响应取消。资源要在真正拥有它的一层注册和关闭。

## 6. 完成标准与常见问题

交付插件前，验证目录加载、安装后加载、准确的工具输出、options/services、具名就绪检查，以及与功能相关的非法输入和清理路径。缺失必需资源时应明确失败。真实模型测试单独进行；脚本模型示例验证接入，不验证模型质量。

| 现象 | 检查什么 |
|---|---|
| `No installed plugin is named ...` | 本地目录用 `./src/text_tools`，或安装带 `pi_python.plugins` 入口点的 wheel。裸名字表示已安装插件。 |
| 相对导入失败 | 加载目录，不要单独加载它的 `plugin.py`。 |
| options 或必需检查缺失 | 使用解析后的插件名：目录 basename 或入口点的 key。 |
| 技能没出现 | wheel 应包含 `SKILL.md`，frontmatter 要有非空 description；启用严格加载并显式要求该技能。 |
| 模型直接收到 `/count` | 先调用 `plugins.expand(...)`。 |
| 找不到新 API | 检查安装版本和能力声明，安装预期 checkout 或构建产物。 |
| MCP 加载看似成功但没有工具 | 严格加载，并在 readiness 中要求对应服务/工具。默认加载可能只警告并跳过不可用服务。 |

本仓库已有可直接运行的多资源示例：`uv run python examples/plugin_demo.py`。它还展示了读取技能和调用 reviewer 子 Agent，源码见 [lab_tools](../../examples/plugins/lab_tools)。
