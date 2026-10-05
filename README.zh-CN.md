# pi-python-core

[English](README.md) | **中文**

一个可嵌入的 Python agent 核心，移植自 [Pi](https://github.com/earendil-works/pi)（固定参照 `v1.0.0`）。它负责一件事：把对话发给模型，执行模型要求的工具，把结果交回模型，直到得到回答。工具就是普通的 Python 函数；模型可以是 Claude、GPT、DeepSeek，也可以是本机或集群上的开源模型。

PyPI 上的 `pi-agent-core` 是另一个独立项目，移植的是 2026 年初 pi-mono 中的旧版本。本库对照 Pi v1.0.0 的行为，与上游的实际运行结果逐组比较；并自带 Claude、OpenAI、DeepSeek 和本地模型的接入，不需要安装任何模型 SDK。

## 安装

支持 Python 3.11–3.14（包括无 GIL 的 3.14t）和 PyPy 3.11，不需要 Node 或任何模型 SDK。

```bash
pip install pi-python-core      # 或 uv add pi-python-core
```

安装名是 `pi-python-core`，导入名是 `pi_python`。

| 安装选项 | 带来什么 |
|---|---|
| 不加选项 | 执行核心和内置模型接入：Claude、OpenAI、Codex、DeepSeek，以及任何 OpenAI 兼容服务（Ollama、vLLM、llama.cpp 等） |
| `[oauth]` | 用 ChatGPT 账号登录（`openai-chatgpt`）时校验身份令牌，会带进需要编译的 `cryptography`。Claude 订阅和 Codex 登录不需要它 |
| `[providers]` | 与 `[oauth]` 相同，让 0.8.1 及以前的安装命令仍然可用 |
| `[mcp]` | 把 MCP 服务器的工具交给 agent，插件声明的 MCP 服务器也需要它 |
| `[mcp-interactive]` | 允许 MCP 服务请求宿主模型调用及用户表单，支持配置选择、计量与重试。见 [MCP 交互](docs/zh/MCP_INTERACTION.md) |

依赖写的是版本范围而不是固定版本，能和大多数已有环境共存。

## 五分钟上手

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

`@tool` 从函数签名和文档字符串生成工具；普通函数和 `async` 函数都可以。在异步程序里用 `await agent.prompt(...)`。换成本地模型只改两行：

```python
from pi_python.providers import OpenAICompletionsProvider

llm = OpenAICompletionsProvider(base_url="http://localhost:11434/v1", name="ollama")
agent = Agent(provider=llm, model=llm.model("qwen3:8b", context_window=40960), tools=[word_count])
```

不联网也能先跑起来：`python examples/quickstart.py`。

## 能做什么

| 需要 | 怎么做 | 示例 |
|---|---|---|
| 写工具 | `@tool` 装饰普通函数；pydantic 模型、dataclass、枚举、日期等参数自动转换；也接受手写或 MCP 生成的 JSON Schema | [quickstart](examples/quickstart.py) |
| 接模型 | Claude 与 GPT 的 API key 或订阅登录；DeepSeek；任何 OpenAI 兼容服务 | [local_model](examples/local_model.py)，[provider_chat](examples/provider_chat.py) |
| 用 MCP 工具 | `async with connect_stdio(...) as tools`；远程服务器用 `connect_http(url)` | [mcp_tools](examples/mcp_tools.py) |
| 让一个 agent 调用另一个 | 把子 agent 包成工具，取消会一路传下去；插件也可以用 Markdown 定义子 agent | [subagent](examples/subagent.py) |
| 打包和分享能力 | 插件把工具、说明、钩子、技能、提示模板、子 agent 和 MCP 服务器打成一组；用 `load_plugins` 按名字或路径加载，再用 `plugins.agent(...)` 构造 agent | [plugin_demo](examples/plugin_demo.py) |
| 中途干预 | `steer` 插入指导，`follow_up` 排队后续任务，`abort` 随时取消（任何线程都可以调用） | |
| 上下文满了、服务出错 | `is_context_overflow`、`is_retryable_error` 判断原因，`continue_run()` 重试，`transform_context` 压缩 | [recovery](examples/recovery.py) |
| 保存与恢复对话 | `encode_messages` / `decode_messages`，应用决定存在哪里 | [save_restore](examples/save_restore.py) |
| 观察与审计 | 订阅事件；执行前后的钩子可以阻止、改写工具调用 | |

除 `provider_chat` 需要真实凭据外，示例都能离线运行，不需要 API key；测试会逐个运行它们。

## 与 Pi 的关系

执行循环、事件顺序、钩子、队列、出错与取消的处理都与 Pi 一致，并用差分验证：同一组输入分别交给固定版本的上游代码和本库运行，逐项比较模型请求、工具调用、事件和最终记录。目前核心循环 25 组、模型接入 50 组、WebSocket 多轮 2 组、出错判断 44 条样例全部一致。插件里技能、提示模板和文件头元数据的规则，也用同样的方法和 Pi 的 coding-agent 代码对照。

有几处是有意的差异，例如工具参数严格校验、不自动转换类型，返回给调用者的状态是副本；另一些是为 Python 用户加的，例如 `@tool`、同步调用、失败后直接 `continue_run()`。逐项记录见[验收映射](compat/COVERAGE.md)。Pi 放在应用层的功能（终端界面、会话文件格式、上下文压缩）不在本库核心里；压缩可以用钩子实现，示例里有完整做法。插件是从应用层取来的唯一一块：一个可选模块，读取 Pi 的 package 格式，再据此构造普通的 Agent。本项目使用自己的版本号，并非 Pi 官方发行版。

## 文档

- [一页看懂：五个概念和一轮的流程](docs/zh/CONCEPTS.md)
- [公开 API](docs/zh/API.md)
- [模型接入、订阅登录与本地模型](docs/zh/PROVIDERS.md)
- [插件：技能、提示模板、子 agent 和 MCP 服务器](docs/zh/PLUGINS.md)
- [实施与验证结果](docs/IMPLEMENTATION.md)
- [与 Pi 的逐项对照和有意差异](compat/COVERAGE.md)
- [参照重建与候选版本验证](reference/README.md)

## 开发

```bash
uv sync --locked --extra oauth --extra mcp-interactive
uv run pytest -q
uv run python scripts/verify.py      # 全部检查，含与上游的差分（需要 Node）
```

CI 配置在 `.github/workflows/ci.yml`，每次推送都在 GitHub Actions 上运行，覆盖 Linux 上的各个 Python 版本（含 3.14t 和 PyPy）以及 macOS 和 Windows。
