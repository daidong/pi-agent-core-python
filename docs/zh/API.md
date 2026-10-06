# Python API

[English](../API.md) | **中文**

从 `pi_python` 导入公开类型。最低 Python 版本为 3.11，只使用 `asyncio` 后端。先读 [一页概念](CONCEPTS.md)。`Agent` 通过 Provider 发起模型请求，不隐式读取密钥，也不关闭调用者传入的共享 Provider。真实接入见 [模型接入](PROVIDERS.md)。

## Agent 与运行结果

`Agent(provider=..., model="mock" 或 ModelInfo(...), options={}, system_prompt="", tools=[], messages=[], hooks=Hooks(), limits=RunLimits(), execution_mode="parallel", steering_mode="one_at_a_time", follow_up_mode="one_at_a_time")`。既没有 `provider` 也没有用 `set_default_stream_fn` 设置默认流时，`prompt` 直接抛 `ConfigurationError`。

| 方法 | 行为 |
|---|---|
| `await prompt(str_or_message_or_list)` | 追加输入并运行，返回 `RunResult` |
| `await continue_run()` | 从合法的用户/工具结果历史继续，或消费最终回答之后的排队输入；最后一条回答失败或被取消时，重试这一轮（见“从失败中恢复”）；不重放历史工具 |
| `prompt_sync(...)` / `continue_run_sync()` | 普通脚本里的阻塞版本，按 Ctrl+C 会取消本次运行；见“在普通脚本里使用” |
| `steer(message)` | 在下一安全边界接纳指导；也接受字符串 |
| `follow_up(message)` | 在工具和指导不再要求继续时接纳后续输入 |
| `abort(reason="requested")` | 请求取消，立即返回；运行随后按 Pi 的方式收尾，见下文“取消、超时与事件” |
| `await wait_for_idle()` | 等待本地任务和关键结束订阅；清理超时抛异常 |
| `subscribe(listener)` | 注册同步或异步关键订阅者，返回取消订阅函数 |
| `update_config(AgentConfigUpdate(...))` | 按字段替换工具、模型或选项；运行中排到下一轮边界 |
| `clear_queues(steering=True, follow_up=True)` | 显式清除指定队列 |
| `await aclose()` | 请求取消并等待本实例的清理，不关闭共享 Provider |

同一实例不接受重叠的 `prompt` / `continue_run`，第二次调用立即抛 `AgentBusyError`。支持 `async with Agent(...)`。`state` 返回防御性复制；修改它不会改变历史。

`RunResult` 包含 `status`（`completed` / `failed` / `cancelled` / `limit_reached`）、本次新增 `messages`、数值用量累计 `usage`、`stop_reason`、`errors`、`tool_outcomes`、`reconciliation_required`、`cleanup_complete` 和两个队列的剩余数量。用量只累加 Provider 报告的顶层数值字段；不推算费用。

`tool_outcomes` 按调用顺序记录原始参数、准备后参数、`raw_result` 和最终 `result`。`execution_status` 是 `not_started`、`running`、`succeeded`、`failed`、`cancelled`、`unknown`。`cancelled` 表示工具被取消或超时；`unknown` 只来自工具主动抛出的 `ToolOutcomeUnknownError`。执行成功但输出检查或后置钩子失败时，状态仍为 `succeeded`，最终结果为错误。所有错误都不触发自动重试。

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

`stream` 返回异步迭代器，调用方不先 await。`ModelRequest` 包含 `messages`、重放后的 `tools`、`model`（名称）、调用者给出的 `model_info`（可为 None）和 `options`；它们是与运行配置隔离的副本。Provider 可以修改自己的副本，不能反向改变历史。

只有一套事件协议，与上游相同：可选的 `start`；每个内容块依次是 `*_start`、若干 `*_delta`、`*_end`（块类型为 text、thinking、toolcall）；最后恰好一个 `done`，携带完整回答。构造函数为 `ModelEvent.boundary("start"|"end", index, block)`、`ModelEvent.text(delta, index)`、`ModelEvent.thinking(delta, index)`、`ModelEvent.toolcall(json_fragment, index)` 和 `ModelEvent.done(message)`；`index` 是该块在最终回答中的位置。不流式的 Provider 可以只发 `done`。失败可以直接抛异常，也可以发 `error` 事件（远程 Provider 都这样做）。两种情况下，Agent 都和 Pi 一样，把这次回答记成一条 `stop_reason="error"` 的消息：保留已经流出的部分内容、供应商与模型名和错误文字，然后照常调用 `finish_turn`、发出 `turn_end`。

每个事件都会经过同一个检查：块必须成对，增量必须落在同类的打开块里，`done` 必须是最后一个事件且迭代器正常结束，最终回答必须与已结束的块一致（只允许上游的推理加密内容补全）。不满足时本轮失败，不执行任何工具。常规完成原因是 `stop`、`tool_use`、`length`、`error`、`aborted`。

消息数据还允许 `pending` 和 `deferred`。`pending` 只用于流中快照，不能提交历史；`deferred` 可保存后台句柄，但本库尚无后台任务轮询。`length` 中的工具全部产生未执行结果，然后允许模型处理错误。`error` / `aborted` 消息不能声明工具调用：提交前去掉其中的工具调用，保留其余部分内容，并在 `diagnostics` 记一条 `removed_tool_calls`。

Agent 在每次请求结束时关闭该请求的响应迭代器，包括出错和取消路径。持有资源的自定义迭代器应实现 `aclose()`；共享 Provider 仍由应用关闭。

## Tool

`Tool(name, description, input_schema, execute, output_schema=None, execution_mode="parallel", prepare_arguments=None)`。

`execute(args, context)` 可以是异步函数，也可以是普通函数。普通函数在工作线程里运行，不会阻塞事件循环。线程无法被中断：取消后，库会在清理期限内等它结束，结束了就采用它的结果；超过期限就按“工具没能停下”处理（见“取消、超时与事件”）。耗时长的工具建议写成异步函数。`ToolContext` 提供 `run_id`、`call_id`、`cancel` 与 `await emit_update(json_value)`。

工具可以返回 `ToolResult`，也可以直接返回普通值：字符串成为文本结果；`None` 成为空内容；字典、列表和数字成为它们的 JSON 文本，同时作为 `structured_content`。

`ToolResult.text(text, details=None, structured_content=None, is_error=False, terminate=False)` 创建文本结果。`details` 和 `structured_content` 不自动发给模型。内容接受 `TextContent` 和 `ImageContent`。输入默认严格验证，不转换类型、不删除 null。`prepare_arguments(args)` 可同步或异步返回新参数，随后统一验证；原始和转换后参数均保留。

接受标准 JSON Schema：按 `$schema` 选择 draft-04、06、07、2019-09 或 2020-12，未写时按 2020-12。pydantic 生成的 schema 和常见 MCP 工具的 schema 都可以直接用。与 Pi 相同，校验参数时检查 `pattern` 和 `format`。`format` 在 jsonschema 有对应检查器时才检查：`email`、`date`、`ipv4` 等直接可用，`uri`、`date-time` 等需要另装 `jsonschema[format-nongpl]`。只允许指向 schema 内部的 `$ref`，引用网络或文件会在注册时被拒绝。schema 中的对象和数组最多嵌套 100 层（每个对象或数组算一层，所以一层 `properties` 嵌套占两层），更深的在注册时报 `ConfigurationError`。

### 从函数生成工具

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

`@tool` 把函数变成 `Tool`：函数名是工具名，文档字符串的第一段是说明，参数类型生成输入 schema，Google、NumPy 或 Sphinx 风格的参数说明写进 schema。也可以写成 `@tool(name=..., description=..., execution_mode=..., output_schema=...)`，或对已有函数调用 `tool(fn)`。

支持的参数类型：`str`、`int`、`float`、`bool`、`None`、`list`、`set`、`tuple`、`dict[str, T]`、`Literal`、`Enum`、`Optional` 和其他联合类型、`Annotated[T, "说明"]`、`TypedDict`、dataclass、`datetime`、`date`、`UUID`、`Path` 以及 pydantic 模型。调用前，JSON 参数会转换成函数要求的类型，例如枚举成员、日期、dataclass 或 pydantic 模型。标注为 `ToolContext` 的参数会收到调用上下文，不出现在 schema 里。`*args`、`**kwargs` 和无法表示成 JSON 的类型在注册时报 `ConfigurationError`。

联合类型按注解中的顺序尝试转换，但只尝试 JSON Schema 与输入匹配的分支。例如，`list[int] | str` 会把 `"abc"` 保留为字符串。多个分支匹配时，采用第一个转换成功的分支。

注册时同时生成 schema 和转换规则，包括递归类型和联合类型分支。未指定元素类型的 `set`、`frozenset`、`tuple` 也会把 JSON 数组转换为对应的 Python 容器。不同参数及联合类型分支中的 pydantic 嵌套定义互相隔离，同名类不会覆盖彼此。

`TypedDict` 的必填与可选字段按解析后的 `Required`、`NotRequired` 判断；延迟类型注解、继承字段和 `Annotated` 包装也遵守同一规则。

并发数默认不设上限，同一批工具全部同时执行，与 Pi 相同；需要时设置 `RunLimits(max_concurrency=...)`。任一工具要求 `sequential`，整个批次串行。并发时按调用顺序完成准备，再启动执行；结束事件按后置整理完成顺序，历史结果按调用顺序。

并行工具和子代理批次负责其子任务的生命周期：发生失败或取消时，取消尚未完成的其他任务，并等待它们清理。调用方重复取消不会中断清理。Run 原有的清理期限仍然有效，`cleanup_complete` 表示清理是否实际完成。

工具内用 `await context.run_agent(child, message)` 调用自己独占、当前空闲的子 Agent。它返回子 Agent 的 `RunResult`，并关闭该 Agent，共享 Provider 仍由应用管理。见[子 Agent 示例](../../examples/subagent.py)。正在运行的子 Agent 会被拒绝，不会被取消或关闭。普通模型或工具失败仍作为结果返回，由调用工具处理。

嵌套调用分别保留两个事实：外部操作结果是否确定，以及工作是否已经停止。子 Agent 的结果未知时，抛出 `ToolOutcomeUnknownError`，同时标记父 Agent 需要人工核对并停止后续执行；并行任务和多层嵌套也遵守这项规则。子任务尚未结束时，外层工具继续持有它。父运行仍按自己的清理期限返回，但会报告 `cleanup_complete=False`，直到后代任务真正结束。重复取消不会丢弃这项清理工作。

独立使用 `ToolContext` 时，应持续等待该调用，或用 `TaskScope` 管理它的生命周期；没有外层 Agent 时，就没有 Run 的清理期限。依赖此 API 时可检查 `require_features(["nested-agent-ownership-v1"])`。

独立程序可调用 `await run_tool_call(tool, call, context, before_tool_call=..., after_tool_call=...)`，复用同一验证与钩子路径。独立调用的生命周期、超时和取消任务由程序自己管理；`Agent` 提供批次管理和 `RunLimits`。

## 钩子

所有钩子可以是同步函数或异步函数。上下文与参数均以副本传入；取消令牌和工具更新通道为共享控制接口。

| 钩子 | 参数与返回值 |
|---|---|
| `prepare_request(context, cancel)` | 返回 `TurnUpdate` 或 None，在每次模型请求前调用 |
| `prepare_next_turn(context, cancel)` | 返回 `TurnUpdate` 或 None，从第二轮开始调用 |
| `transform_context(messages, cancel)` | 返回模型请求使用的消息列表，先于 convert |
| `convert_to_llm(messages)` | 返回可送模型的消息列表，必须处理或显式过滤 `CustomMessage` |
| `before_tool_call(call, args, context)` | True / None 允许，False 阻止，或返回无需执行的 `ToolResult`；抛异常时该调用得到错误结果（`hook_error`），运行继续，与 Pi 相同 |
| `after_tool_call(call, result, context)` | 返回完整 `ToolResult`、部分 `ToolResultUpdate` 或 None |
| `finish_turn(context, cancel)` | 返回 `"continue"`、`"end"` 或 None |

准备和结束钩子的 `context` 是 `RunContext`，包含 `messages`、`model`、`options`、`tools`、最近的 `message` 和 `tool_results`。`TurnUpdate(model=..., options=..., tools=..., context=..., messages=...)` 持续影响本次 run，不改 Agent 默认值。`context` 替换本次运行的模型上下文，不改写已提交历史；`messages` 是新追加输入。显式 `update_config` 才更新下一次 run 的默认配置。选项按字段完整替换，不深合并。

后置钩子用 `ToolResultUpdate(content=[...])` 替换文本时，旧 `structured_content` 被清除；如需要保留，应同时提供新结构化结果。成功结果在后置钩子前后均检查输出 schema。`terminate=True` 必须在整个批次的最终结果中全部成立，才抑制工具引发的下一请求；排队输入仍可触发继续。`finish_turn="end"` 不消费待处理队列。

## 取消、超时与事件

`RunLimits` 默认不限制模型请求数、工具调用数和工具并发数，与 Pi 相同；应用需要时自己设置 `max_model_requests`、`max_tool_calls` 和 `max_concurrency`。清理期限默认 1 秒。`tool_timeout`、`run_timeout` 默认为 None，以单调时钟计时。设置了工具调用上限时，在批次开始前检查，不够时整批不执行。

显式 `abort` 按 Pi 的方式结束运行。`abort` 可以在任何线程调用，例如图形界面的停止按钮或看门狗线程：

- 取消通过 asyncio 的任务取消，送达正在运行的模型流、工具和钩子，作用相当于 Pi 的 AbortSignal。每个操作在清理期限内按自己的方式结束。
- 模型流被取消时，已经流出的部分内容记成一条 `stop_reason="aborted"` 的回答，照常调用 `finish_turn` 并发出 `turn_end`。
- 工具被取消时，记成普通错误结果 `Operation aborted`（`execution_status="cancelled"`）。工具捕获取消后自己返回的结果照常采用，已完成的结果保留。整批结果提交后，与 Pi 一样再进入一轮：这一轮不发模型请求，直接记一条 aborted 回答，然后结束。
- `prompt` 返回 `status="cancelled"`，Agent 可以继续使用。

调用者取消 `prompt` 所在的 Python Task 时，不走上面的流程：库进行受限清理，然后重新抛 `asyncio.CancelledError`。

运行在模型回答之外的地方失败或被取消时（例如准备钩子抛错），和 Pi 的 `handleRunFailure` 一样追加一条 `error` / `aborted` 回答并发出 `turn_end`。订阅者故障除外：此时停止发布，不再追加。

工具超时同样取消该工具，结果是错误 `tool_timeout`，交给模型处理，运行继续。

工具不能确认外部提交是否成功时，可以主动抛 `ToolOutcomeUnknownError`。这是本库在 Pi 之外加的保护：它停止当前运行，并拒绝后续 prompt、continue 和配置更新，由应用在外部对账后创建带修正历史的新实例。取消和超时不会触发这项保护。

不合作的协程和普通函数所在的线程都不能被 Python 强制终止。取消或超时后，工具在清理期限内仍未结束时，它的结果是错误 `not_stopped`，运行立即停止，不再发模型请求。在它真正结束之前，实例保持非空闲状态，`cleanup_complete=False`，`prompt`、`wait_for_idle` 和 `aclose` 抛 `CleanupTimeoutError`；它一结束，实例自动恢复可用。应用若需要强制终止，应自行管理子进程。超时或取消结果不代表外部操作已回滚。

事件包括 `agent_start/end`、`turn_start/end`、`message_start/update/end`、`tool_execution_start/update/end`，以及显式配置变化的 `config_update`。信封包含 `schema_version=1`、run ID、turn ID、单调序号和可选调用 ID。每个订阅者收到独立副本。订阅者按顺序等待，失败时停止执行并在 `state.diagnostics` 记录失败者与未处理订阅者；不会递归调用失败的订阅者。

订阅回调不得等待本实例 idle，否则会形成循环等待。显示层可用有界 `EventQueue(maxsize=128, drop_text_updates=True)`，只有消息增量可丢弃，计数保存在 `dropped_updates`，工具最终事件始终采用背压。正常路径的 `prompt` 返回意味着结束事件的关键订阅完成。

## 从失败中恢复

模型出错或被取消时，这次回答以 `stop_reason="error"` 或 `"aborted"` 留在历史里，`error` 字段是错误原文；HTTP 失败时还带服务器返回的正文（最多 4000 字符，本次请求用的密钥会被替换成 `[redacted]`）。下面这些函数从上游 pi-ai 移植，读这条回答来判断原因，44 条共享样例的判断结果与上游一致：

| 函数 | 作用 |
|---|---|
| `is_context_overflow(message, context_window=None)` | 是否因上下文超长而失败。传入窗口大小时，还能识别静默超长（报告的输入超过窗口）和截断后零输出的长度停止 |
| `is_retryable_error(message)` | 是否像临时错误：过载、限流、5xx、网络中断、流提前结束。配额和账单问题不算 |
| `is_recoverable_length(message, desired_max_output)` | 长度停止但输出少于期望上限，可能是上下文压力所致 |
| `retry_delay(attempt, base=2.0, max_delay=60.0)` | 第 `attempt` 次重试前等待的秒数，指数退避 |

`continue_run()` 在最后一条回答失败或被取消、且没有排队输入时，重试这一轮。失败的回答留在历史里，向模型重放时会被跳过。这一点与 Pi 不同：Pi 的应用层会先删掉失败的回答再继续。[examples/recovery.py](../../examples/recovery.py) 演示了完整做法：超长时先用 `transform_context` 把较早的几轮换成摘要再继续，临时错误时按 `retry_delay` 退避后继续。

## 在普通脚本里使用

`agent.prompt_sync(...)`、`agent.continue_run_sync()` 和通用的 `run_sync(awaitable)` 在普通脚本里阻塞运行，不需要自己写 `asyncio.run`。它们共用一个后台事件循环，所以 Provider 缓存的连接在多次调用之间仍然可用。按 Ctrl+C 时，本次运行会按 abort 的方式收尾，然后抛出 `KeyboardInterrupt`。

在已经运行事件循环的地方（异步程序、Jupyter 中使用顶层 await），这些函数会报错，并提示改用 `await agent.prompt(...)`。同一个 Agent 请只用一种方式调用：要么全用阻塞版本，要么全在自己的事件循环里 await。

## MCP 工具

```python
from pi_python.mcp import connect_stdio

async with connect_stdio("uvx", ["mcp-server-fetch"], prefix="web") as tools:
    agent = Agent(provider=..., tools=tools)
    await agent.prompt("Summarize https://example.org")
```

需要 `pip install 'pi-python-core[mcp]'`，兼容官方 MCP SDK 1.10 及以上和 2.x。`connect_stdio` 启动一个 stdio MCP 服务器，退出 `async with` 时关闭它。`connect_http(url, headers=None, prefix=None, names=None)` 用同样的方式连接 streamable HTTP 服务器；和 Pi 一样，不支持旧的 SSE 传输方式。它只在服务器的同一源内跟随重定向，header 不会被发到其他源。它在 MCP SDK 1.10、1.30 和 2.3 上测过。已经自己建立了 `ClientSession` 时，用 `await mcp_tools(session, prefix=None, names=None)` 包装它的工具。转换方式与 Pi 的 MCP 适配相同：文本和图片直接转换；嵌入的文本或图片资源取出内容；音频、资源链接和二进制资源换成简短的文字说明；只有结构化结果时转成 JSON 文本，同时作为 `structured_content`；MCP 的 `isError` 成为工具错误；进度通知成为 `tool_execution_update` 事件。工具名加上前缀后只保留字母、数字、`_` 和 `-`，最长 64 个字符。schema 无法使用的工具会发出警告并跳过，不影响同一服务器的其他工具。用 Python MCP SDK 2.3 写的 stdio 服务器在 PyPy 上无法启动（缺少 `fcntl.F_DUPFD_CLOEXEC`），这是 SDK 本身的限制；客户端一侧不受影响。

三个 MCP 入口均接受可选的 `call_metadata`，把 JSON 对象传给
`ClientSession.call_tool(meta=...)`。注册时和每次调用时独立复制，不加入工具参数。
SDK 不支持 `meta` 时，注册会抛出 `ConfigurationError`。
插件配置和启动验收见[插件指南](PLUGINS.md#mcp-服务器)。

在 POSIX 系统上，`connect_stdio(..., process_scope=True)` 还会在退出时清理残留进程组
和嵌套的 pi-python stdio 连接。外层连接显式启用，内层连接自动继承。
非 POSIX 系统不接受显式启用，具体范围见 [进程归属与限制](MCP_INTERACTION.md)。

`connect_stdio` 和 `connect_http` 还接受 `callbacks=MCPCallbacks(...)`，按宿主授权启用
模型请求和表单交互。`SamplingHandler` 接入宿主 Provider，`SamplingProvider` 让服务端 Agent
通过当前 MCP 请求使用模型。新能力要求 SDK >=2.3,<3；公共接口、协议限制、取消及可运行示例见
[MCP 交互](MCP_INTERACTION.md)。

## 插件

`pi_python.plugins` 负责加载插件。插件是一组有名字的附加内容：工具、系统提示文字、钩子、技能、提示模板、子 agent 和 MCP 服务器，格式沿用 Pi 的 package。`async with load_plugins([...], services=..., options=...) as plugins:` 加载插件，`plugins.agent(...)` 构造一个带上全部插件内容的 Agent。怎样编写、发布插件，以及多个插件怎样合在一起，见 [插件指南](PLUGINS.md)。

| 名称 | 作用 |
|---|---|
| `load_plugins(sources, services=None, options=None, on_error=None)` | 返回 `PluginSet`；用 `async with` 打开，普通脚本里用 `with` |
| `PluginSet.agent(**Agent 的参数)` | 在你的配置上加入插件的系统提示文字、工具、钩子和事件监听，返回 Agent |
| `PluginSet.expand(text)` | 展开 `/skill:name 参数` 和 `/模板名 参数`，其他文字原样返回 |
| `PluginSet.system_prompt(base)`、`.tools`、`.hooks(base)`、`.skill_tool()`、`.subagent_tool(tools=(), hooks=None, provider=None, stream_fn=None, model=None, limits=None)` | 各个部分，供自己构造 Agent 时使用。不给 `model` 时，子 agent 用调用那一刻主 agent 的模型；`limits` 作用于每次子 agent 运行 |
| `await PluginSet.check()` | 运行插件的自检，返回 `CheckResult(plugin, name, passed, detail)` 列表 |
| `PluginSet.skills`、`.prompts`、`.agents`、`.plugins`、`.diagnostics` | 加载了什么：`Skill(name, description, path, plugin, disable_model_invocation)`、`PromptTemplate(name, description, content, path, plugin, argument_hint)`、`AgentDefinition`、`Plugin`，以及警告文字 |
| `Plugin(name, setup=None, root=None, version=None, source="code")` | 在代码里定义的插件。`root=None` 表示没有资源目录；通过入口点导出的 `Plugin` 则表示导出它的那个包的目录 |
| `PluginAPI` | 插件的 `setup(api)` 收到的对象 |
| `AgentDefinition(name, description, system_prompt="", tools=None, model=None, options={}, provider=None, path=None, plugin="")` | 一个子 agent；`path` 和 `plugin` 记录定义的来源 |
| `discover_plugins()` | 以 `InstalledPlugin(name, target, distribution, version)` 列出已安装的插件，不导入它们；插件注册在入口点组 `ENTRY_POINT_GROUP`（`"pi_python.plugins"`）里 |
| `PluginFailure`、`PluginWarning` | 插件处理函数出错时 `on_error` 收到的报告（不是异常）；某项资源被跳过时的警告 |

## 消息与编码

公开消息类型为 `SystemMessage`、`UserMessage`、`AssistantMessage`、`ToolResultMessage`、`CustomMessage`，内容块为 `TextContent`、`ImageContent`、`ThinkingContent`、`ToolCall`。用户和工具结果可带图片；助手可带签名文本、推理、工具调用。`SystemMessage` 的文本追加指令，`sections` 按名替换、None 删除；工具声明通过 `tools_added` / `tools_removed` 重放。

`encode_messages` / `decode_messages` 和 `encode_event` / `decode_event` 是纯字符串转换，不读写文件。消息 schema 版本为 3，事件信封版本为 1，其他版本一律拒绝。不接受 NaN、Infinity、函数或文件句柄；拒绝悬空调用、重复结果和未知版本。历史导入只校验数据，不执行模型或工具。`current_tools`、`current_system_message`、`current_system_prompt` 可用于外部适配器的状态重放。


## 独立循环与新增便利接口

`agent_loop(prompts, AgentContext(...), AgentLoopConfig(...), cancel=None)` 和 `agent_loop_continue(context, config, cancel=None)` 返回 `AgentEventStream`，支持 `async for` 和 `await stream.result()`。`run_agent_loop(..., emit=None, cancel=None)` / `run_agent_loop_continue(...)` 直接返回新增消息，支持同步或异步事件回调。它们复用 Agent 的执行引擎。continue 版本将历史写回传入 context；prompt 版本不修改原 context，与上游对应。

配置包含 `provider` 或 `stream_fn`、`model`、`options`、`hooks`、`limits`、`tool_execution` 及 `get_steering_messages` / `get_follow_up_messages`。队列回调在上游对应边界调用，可以同步或异步返回消息列表。

事件流使用有界队列。只等待结果时 `result()` 会消费事件；使用 async for 时应消费到结束，再读取结果。中途放弃应调用 `await stream.aclose()`；不要在暂停消费期间仅等待生产者结束，否则背压会阻塞。

`set_default_stream_fn(provider_or_function)` 设置显式进程级默认流，传 None 清除。`Agent` 可省略 provider，也可用 `stream_fn=`。`reset()` 清除对话和队列，但保留重放后的系统提示与工具声明；运行中拒绝 reset。工具主动报告结果未知的实例仍需对账后重建，reset 不清除此保护。

`has_queued_messages()`、`peek_queued_messages()`、`clear_steering_queue()`、`clear_follow_up_queue()`、`clear_all_queues()` 对应上游便利方法。预览优先 steering，只有它为空才预览 follow-up，返回副本。`signal` 在运行中返回活动 `CancelToken`，空闲时为 None。`prompt(text, images=[...])` 支持图片快捷入口。

构造 Agent 还可传 `thinking_level`、`thinking_budgets`、`transport`、`session_id`、`get_api_key`、`on_payload`、`on_response`、`on_provider_stream_event`。后四项也可放在 Hooks 中，明确的构造参数优先。它们的协议与 Provider 参数详见 [接入说明](PROVIDERS.md)。

## 消息、工具结果与事件的字段

`SystemMessage.content` 接受字符串或文本块列表。`AssistantMessage` 还包含 `response_model`、`response_id`、`thinking_level`、`diagnostics`、`raw_stop_reason`、`end_turn` 和 `deferred`。`provider_thinking_level` 是原生 effort，`thinking_level` 是请求等级。任意用户 JSON 中值为 null 的字段完整保留。

`ToolResult`、`ToolResultUpdate` 和历史 `ToolResultMessage` 支持 `usage`、`nested_calls`；历史还保存 `details`。嵌套调用记录格式为 `{complete: bool, calls: [{id, name, status, ...}]}`，status 为 ok/error/unfinished。details、usage、nested_calls 在发送主模型前剥离。工具执行和后置钩子通过 `ToolContext` 取得 assistant_message、agent_context、tool_call、args、result 和 is_error；`RunContext.new_messages` 收集本轮运行新增消息。所有这些上下文都是副本。

经过检查的事件中，非终止事件携带 `partial`（独立快照，不随下一事件改变）；块边界携带 `block`，文本/推理结束携带 `content`，工具增量携带该块的 `call_id` 和 `name`，终止事件携带 `message` 和 `reason`。工具参数预览可不完整，但执行使用严格解析的最终参数。Agent 的 `message_update` 事件数据为 `delta_type`、`block_index`、`delta`、`content`、`tool_call_id` 和 `partial`。

OpenAI 可在最终响应中补回 encrypted_content。若可见推理和其余签名字段完全相同，仅补上缺失的加密内容，则允许终止消息比块结束快照多出该字段。文本、参数和工具身份仍须一致。

## 会话变更与上下文估算

`render_system_update(message)` 返回后续系统消息原位发送时的文本：正文之后，每个段落写成 `Updated system prompt section "名称":` 加新内容，或 `Removed system prompt section "名称".`。一条系统消息由多个文本块组成时，块之间用单个换行连接，与上游 `contentText` 相同。会话中途改变工具、指令和推理强度时各 Provider 如何发送，见 [模型接入说明](PROVIDERS.md#会话中途的变更)。

`estimate_context_tokens(messages)` 移植上游的字符估算：最近一次有效用量记录加上其后消息的估算，每 4 个 UTF-16 字符约 1 个 token，每张图片按 4800 字符计。它不是 tokenizer。`clamp_max_tokens_to_context(context_window, messages, max_tokens)` 用它预留 4096 个安全 token，结果至少为 1；Provider 用它截短已知模型的输出上限。

`AssistantMessage.provider_thinking_level` 与上游相同：只在使用推理强度标记的 Claude 模型上记录本次强度，用于在后续请求中重建历史标记。需要请求等级时读取 `thinking_level`。

Provider 重放历史时按上游规则跳过 `error` / `aborted` 回答；跨模型的推理转为普通文本，签名丢弃；一条系统消息位于工具调用和结果之间时移到结果之后。会话历史本身不变。

MCP 的宿主配置、用量观察、重试策略和构建能力标识见 [MCP 交互文档](MCP_INTERACTION.md#真实-provider配置选择与计量)。


## 任务生命周期与同步线程桥接

`TaskScope` 管理传给 `await scope.run(awaitable, cancel=token)` 的异步任务。
每次调用在独立子任务中运行。调用方或取消令牌中止时，框架取消子任务并等待清理。
`await scope.aclose()` 拒绝新调用，取消并等待现有任务，包括等待应用锁的任务。
使用 `async with TaskScope() as scope`，或通过插件的 `api.task_scope()` 注册自动关闭。
执行异常只交给对应调用方，不影响其他调用，也不会在关闭时再次抛出。
任务必须配合取消；框架不会强制终止线程或隐式设置期限。任务不能关闭包含自己的作用域。
一个作用域只能用于首次使用它的事件循环。

`LoopPortal` 让同步工作线程把异步调用交给已有事件循环。它与自行创建后台循环的
`run_sync` 不同。必须在连接所属循环创建并关闭桥接对象：

```python
import asyncio
from pi_python import LoopPortal

async def request(connection):
    async with LoopPortal() as portal:
        return await asyncio.to_thread(portal.call, connection.fetch, "item")
```

`portal.call(async_function, *args, timeout=None, **kwargs)` 接收异步函数而非已创建的协程，
将结果或异常返回工作线程，并保留该线程的上下文变量。任何事件循环线程内的阻塞调用都会被拒绝。
关闭会取消并等待已提交的异步工作，但不关闭借用的循环。
超时限制阻塞等待时间，并请求取消异步工作；清理完成由 `aclose()` 保证。
关闭完成前应保持所属循环运行，工作线程本身仍由应用管理。

`FEATURES` 声明当前构建提供的版本化 API，`require_features(names, where="Application")`
在能力缺失或名称未知时抛出 `ConfigurationError`。这些标记不表示可选依赖已经安装，
也不代表 MCP 对端能力、凭据或宿主授权。原有 `MCP_FEATURES` 保持兼容，是其子集。
插件可声明所需能力，由加载器统一校验，见 [插件文档](PLUGINS.md)。
