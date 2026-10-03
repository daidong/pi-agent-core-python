# 一页看懂 pi-python

[English](../CONCEPTS.md) | **中文**

这个库只做一件事：反复把对话发给模型，执行模型要求的工具，把结果交回模型，直到模型给出回答。理解下面五个概念，就能读懂全部代码。

## 五个概念

| 概念 | 是什么 | 在哪里 |
|---|---|---|
| **消息记录** | 一个只追加的消息列表：用户输入、模型回答、工具结果，以及系统消息。系统指令和工具声明也写成系统消息，所以会话中途的每次变化都留在记录里 | `messages.py`、`transcript.py` |
| **Agent** | 一个会话。保存消息记录、默认配置（模型、选项、工具）、两个输入队列和事件订阅者。一次只运行一个 prompt | `agent.py`、`queues.py` |
| **Run** | 一次 `prompt` 或 `continue_run` 的执行。持有这次运行的取消令牌、后台任务和工具执行记录，结束时整理成 `RunResult` | `run.py`、`loop.py` |
| **Provider** | 一个函数：拿到请求，产出一串模型事件，最后一个是完整回答。Agent 不知道背后是 HTTP、WebSocket 还是脚本 | `provider.py`、`providers/` |
| **Tool** | 名称、说明、参数 schema 和一个执行函数（异步或普通函数均可）。通常用 `@tool` 从带类型标注的函数直接生成。参数先严格校验，再执行；结果写回记录 | `tools.py`、`function_tools.py` |

模型能力不是第六个概念，而是 Provider 的输入：`ModelInfo` 记录上下文长度、输出上限、推理等级和服务端支持的特性。给真实 Provider 传模型名时，它在内置模型表里查；表里没有就报错，这时传一个 `ModelInfo`。脚本化的 `ScriptedProvider` 不需要模型表。

## 一轮里发生什么

```text
prompt("…")
  │
  ▼
接纳输入（队列中的指导、工具变化写成系统消息）
  │
  ▼
请求前钩子 ─► transform_context ─► convert_to_llm
  │
  ▼
Provider 流：start → 各内容块 start/delta/end → done
  │            （事件先经检查：块要成对，增量要落在打开的块里，
  │              最终回答要与已结束的块一致；否则本轮失败、不执行工具）
  ▼
回答写入记录
  │
  ├─ 没有工具调用 ──► 看后续队列 ──► 没有就结束
  │
  └─ 有工具调用 ──► 校验参数 → before_tool_call → 执行 → after_tool_call
                     │
                     ▼
                  结果按调用顺序写入记录 ──► finish_turn ──► 下一轮
```

钩子都是可选的。模型出错或被取消时，和 Pi 一样，这次回答（连同已经生成的部分）照常记入历史，走完 `finish_turn` 和 `turn_end`，然后运行结束；达到应用设置的上限时也结束。`RunResult.status` 说明原因。

## 最小例子

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

换成真实模型只改 Provider 和模型名：`Agent(provider=AnthropicProvider(api_key=...), model="claude-sonnet-5-5", ...)`。

## 比 Pi 多出的部分

Pi 把下面这些留给应用；本库为科研和 HPC 场景放进了核心，需要时再读：

- 参数严格校验，不自动把字符串转成数字（需要时写 `prepare_arguments`）；
- `RunLimits`：取消后的清理期限，以及可选的模型请求数、工具调用数、工具并发数、工具超时和运行总时限（这几项默认都不设，和 Pi 一样由应用决定）；
- 工具主动报告结果"未知"（例如作业提交后连接断开）时停止运行，等应用核对，不自动重试；取消或超时一个工具不会触发这项保护，只记成普通错误结果；
- 返回给调用者的状态都是副本，运行中的配置更新在下一轮生效。

细节见 [API](API.md)，模型接入（含本地模型）见 [PROVIDERS](PROVIDERS.md)，与 Pi 的逐项对照见 [验收映射](../../compat/COVERAGE.md)。搭建更完整的 agent 时常用的写法都有可运行的示例：[子 agent](../../examples/subagent.py)、[保存与恢复对话](../../examples/save_restore.py)、[超长时压缩和出错重试](../../examples/recovery.py)、[MCP 工具](../../examples/mcp_tools.py)、[本地模型](../../examples/local_model.py)。
