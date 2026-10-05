# 面向 coding agents 的 pi-python-core 接入指南

[English](../CODING_AGENTS.md) | **中文**

本文用于指导 coding agent **使用本库构建应用**。先运行下面的离线工具调用，再替换模型接入。如果需要可复用的扩展，继续阅读[插件开发流程](PLUGIN_DEVELOPMENT.md)。修改本库自身时使用的检查命令放在最后，接入应用不需要运行整套仓库检查。

## 1. 确认环境

安装名是 `pi-python-core`，导入名是 `pi_python`。需要 Python 3.11 或更高版本，异步运行时使用 asyncio。公开 API 从 `pi_python`、`pi_python.providers`、`pi_python.plugins` 或 `pi_python.mcp` 导入，不要依赖以下划线开头的内部模块。

在使用本库的项目里执行 `uv add pi-python-core` 或 `python -m pip install pi-python-core`。可选安装项：`[mcp]` 接入 MCP 工具，`[mcp-interactive]` 支持宿主模型调用和用户表单，`[oauth]` 用于 ChatGPT 身份令牌验证。内置模型接入不需要额外安装项。

在本仓库工作时，从仓库根目录执行：

```bash
uv sync --locked --extra mcp-interactive
uv run python examples/quickstart.py
uv run python examples/plugin_demo.py
```

这两个示例均可离线运行，不需要凭据。本文对应当前检出的源码；它的版本可能尚未发布。源码版本见 `pyproject.toml`，已安装版本可用 `python -c "from importlib.metadata import version; print(version('pi-python-core'))"` 查看。

另一个项目需要使用本地源码时，在那个项目的环境中执行 `python -m pip install -e /absolute/path/to/pi-python`。需要交互式 MCP 时，在路径后追加 `[mcp-interactive]`。

## 2. 运行一次完整的离线工具调用

保存为 `app.py`，在已安装环境中运行 `python app.py`；在本仓库中可用 `uv run python app.py`。程序输出 `3 words.`，并断言真实工具结果，而不是只检查预先写好的模型回答。

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

`Agent` 管理对话和工具执行循环，`Provider` 提供模型回答。`@tool` 根据函数类型注解和文档字符串生成工具定义。模型选择工具名及 JSON 参数，本库负责校验参数并执行函数。`ScriptedProvider` 把这些选择固定下来，方便确定性测试；它不能证明真实模型也会选择相同的工具或回答。

## 3. 替换模型接入，明确资源归属

接真实模型时，由应用显式传入凭据和选定的模型。在异步函数中，可以用下面的代码替换离线模型；这段会访问网络，可能产生费用，不属于离线示例：

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

`Agent.aclose()` 关闭 agent 自身的工作，不会关闭共享 Provider。所有使用者结束后，由应用关闭 Provider。创建、使用和清理应在同一个事件循环中完成。其他模型接入见 [Providers](PROVIDERS.md)，本地模型有[完整可运行示例](../../examples/local_model.py)。

## 4. 编写应用时遵守这些约定

| 场景 | 正确做法 |
|---|---|
| 异步与同步 | 服务端、notebook 使用 `await agent.prompt(...)`；普通同步脚本使用 `agent.prompt_sync(...)`。不要在事件循环线程中调用阻塞 API，也不要用多次独立的 `asyncio.run` 操作同一个绑定事件循环的 Provider。 |
| 对话归属 | 一个 Agent 同时只接受一次运行。同一对话按顺序 await；独立对话创建不同 Agent。`agent.state` 返回副本。 |
| 成功判断 | 检查 `result.status`：`completed`、`failed`、`cancelled`、`limit_reached`。模型和工具失败不一定抛出 Python 异常；接受业务结果前检查 `errors` 和 `tool_outcomes`。 |
| 工具输入 | 写清参数类型和 docstring。标注为 `ToolContext` 的参数由框架注入，不会进入模型 schema。JSON 参数校验不做类型强制转换。 |
| 工具输出 | 返回兼容 JSON 的值或 `ToolResult`。字典、数字既生成文本，也保留在 `structured_content` 中。`details` 是应用数据，不会自动发送给模型。 |
| 并发与限制 | 一批工具默认并行执行。设置 `RunLimits`；需要顺序执行时用 `@tool(execution_mode="sequential")`。普通函数在工作线程执行，取消不能强行终止该线程。 |
| 取消 | 调用 `agent.abort()` 后等待运行及清理结束。复用 Agent 或重试有副作用的操作前，检查 `cleanup_complete` 和 `reconciliation_required`。可取消的 I/O 优先写成异步工具。 |
| 恢复 | `continue_run()` 可以重试失败的模型轮次，不会重新执行过去的工具；它不是通用的业务操作重试机制。见[恢复示例](../../examples/recovery.py)。 |
| 保存对话 | 保存 `encode_messages(list(agent.state.messages))`，用 `decode_messages` 与 `Agent(messages=...)` 恢复。`result.messages` 只包含本次运行新增的消息。见[保存恢复示例](../../examples/save_restore.py)。 |
| 流式显示 | 用 `agent.subscribe(listener)` 接收 `message_update`，读取 `delta_type` 和 `delta`。关键订阅者会被等待，应保证它及时返回。见 [quickstart](../../examples/quickstart.py)。 |

应用自己的异步工作可以交给 `TaskScope`。同步工具的工作线程需要访问已有异步连接时，由连接所属事件循环创建 `LoopPortal`，再用它提交调用，不要把连接移到 `run_sync` 的独立循环。见 [任务归属和线程调用](API.md)。这些 API 和能力声明属于 0.10.0 源码系列；依赖它们时可用 `require_features(["task-scope-v1", "loop-portal-v1"])` 检查。

## 5. 按需求选择接入方式

| 需要什么 | 从哪里开始 |
|---|---|
| 应用里的几个工具 | `Agent(..., tools=[...])`，不必先建插件。 |
| 可复用工具、说明和资源 | [插件开发流程](PLUGIN_DEVELOPMENT.md)，然后查[插件 API](PLUGINS.md)。 |
| MCP 服务提供的工具 | [MCP 工具示例](../../examples/mcp_tools.py)；Agent 使用工具期间保持连接打开。 |
| MCP 服务请求宿主模型或用户表单 | [MCP 交互](MCP_INTERACTION.md)和[离线示例](../../examples/mcp_interactive.py)，安装交互式 extra，由宿主显式配置回调。 |
| 嵌套 Agent | [子 Agent 示例](../../examples/subagent.py)；插件也可以在 `agents/` 中声明。 |
| 自定义模型适配 | 按 [Provider 协议](API.md) 实现；只发一个终结的 `ModelEvent.done`，并响应取消。 |

MCP 增量采样是双方协商启用的扩展。宿主还原完整请求后才执行策略检查和模型调用。它减少 MCP 重复传输，不减少模型看到的历史或 token；业务代码无需管理内部缓存。

## 6. 交付前验证

对接入应用，先运行离线工具调用，断言实际工具结果，按功能需要验证失败和取消路径，再单独测试配置的真实模型。报告实际跑过哪些路径。插件还要按[开发流程](PLUGIN_DEVELOPMENT.md)验证安装后的 wheel。

修改本库自身时，先读适用的 `AGENTS.md`，遵守任务范围并运行相关测试。仓库检查命令：

```bash
uv run ruff check src tests examples compat scripts
uv run ruff format --check src tests examples compat scripts
uv run mypy src/pi_python
uv run pytest -q
```

精确接口见 [API](API.md)，运行概念见[一页概念](CONCEPTS.md)，涉及上游兼容性的变更见[参照验证](../../reference/README.md)。不要臆造方法，也不要把其他 Agent 框架的 API 套到本库。
