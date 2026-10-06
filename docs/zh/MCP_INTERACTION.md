# MCP 模型请求与用户表单

独立进程里的 MCP 服务可以在执行工具时，请宿主调用模型或向用户提问，再用回复继续执行。
宿主管理模型密钥、授权、界面和运行资源。服务端管理业务提示词、阶段、状态、本地工具和结果验证。
插件无需另建 TCP 服务或自定义回调协议。

安装 `pip install 'pi-python-core[mcp-interactive]'`。新交互能力要求 **MCP SDK >=2.3,<3**，
协商使用 **2025-11-25** 协议。普通工具调用仍支持原有 `[mcp]` 和 SDK >=1.10；
旧 SDK 上启用新能力会明确抛出 `ConfigurationError`。SDK 1.x 的回调调度不满足本实现需要的并发和取消行为。

本版实现[2025 协议的请求内 Sampling](https://modelcontextprotocol.io/specification/2025-11-25/client/sampling)。
[2026-07-28 协议](https://modelcontextprotocol.io/specification/2026-07-28/client/sampling)
已弃用 Sampling，并改用 `InputRequiredResult` 和请求重放。本版没有实现这套多次往返流程，
协商到不匹配的版本会报错。SDK 2.3 仍能使用 2025 协议，但可能输出 Sampling 弃用警告。

## 运行示例

```sh
uv run --extra mcp-interactive python examples/mcp_interactive.py
```

宿主会启动独立 stdio 服务。服务端 Agent 请求模型生成两个本地工具调用，执行后再次请求模型，
然后通过表单征求输出选择。示例的假 Provider 和**模拟用户回答**不需要 API 密钥或终端输入。
把 `DemoProvider` 换成实际 Provider，把 `choose()` 换成宿主的异步界面即可。

HTTP 示例使用两个终端，先启动服务，再运行宿主：

```sh
uv run --extra mcp-interactive python examples/mcp_interactive_server.py --http 8765
uv run --extra mcp-interactive python examples/mcp_interactive.py --url http://127.0.0.1:8765/mcp
```

## 宿主接入

公共 API 都从 `pi_python.mcp` 导入：

```python
from pi_python.mcp import (
    MCPCallbacks, SamplingHandler, ElicitationHandler, ElicitationResponse,
    connect_stdio, connect_http,
)

async def authorize_model(context, request):
    # 可以检查提示词和工具、核对预算，或征求用户批准。
    return context.server == "business"

async def ask_user(request, cancel):
    # 界面显示 request.context.server，并提供审阅、拒绝、取消入口。
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
    ...  # 交给 Agent，或提供 ToolContext 后直接调用。
# connect_http(url, callbacks=callbacks, server_name="business") 用法相同。
```

提供处理器就是宿主对该服务的授权；未提供时不声明对应能力。
`SamplingHandler(allow_tools=False)` 只声明基础模型生成。
表单只声明 `elicitation.form`，不声明 URL 模式。SDK 2.3 注册表单回调时默认会声明两种模式；
适配器通过公开的 `send_request` 接口收窄初始化请求，不替换 SDK 调度器或协议实现。

`MCPRequestContext` 包含宿主指定的服务名 `server`、可选插件名 `plugin`、反向 MCP 请求编号
`request_id`、协议版本、外层 `run_id` 和 `tool_call_id`，以及反向请求自身的 `metadata`。
反向编号不是父请求编号。2025 协议没有统一的反向请求父编号，因此交互连接内同时只执行一个
外层工具调用；回调独立运行，不占外层调用锁。不同连接可以并发。
关联信息不会塞进应用 `_meta` 字段；已有 `call_metadata` 继续独立透传。

回调中不要递归调用同一交互连接，它的外层工具已经在等待该回调。
可调用宿主本地功能或另一连接。宿主指定的服务名是配置身份，不是服务认证证明或权限隔离边界。

## 模型转换与授权

`SamplingHandler` 把请求转换为 `ModelRequest`，调用宿主 `Provider.stream`，验证终止事件，
再转换成 MCP 回复。框架不会关闭这个 Provider。
共享 Provider 时，应复用**同一个 SamplingHandler**；默认 `max_concurrency=1`，串行访问模型。
仅在 Provider 支持重入时提高并发数。宿主同时直接使用同一个 Provider 时，还需要宿主统一调度；
这个限制不覆盖绕过处理器的调用。

| 内容或行为 | 支持范围 |
|---|---|
| 多轮文本、系统提示 | 支持；服务端适配器使用 pi-python 原有方法合并系统提示及 sections |
| 工具声明 | 名称、描述、输入 schema；其他声明字段明确拒绝 |
| 多个工具调用及结果 | 保留编号和名称；缺失、重复、错配或混入普通消息时拒绝 |
| 工具选择 | `auto`、`required`、`none`；Anthropic 设置 `tool_choice_format="anthropic"`，其他默认 OpenAI 写法 |
| 用户消息、工具结果中的图片 | 宿主显式启用 `allow_images=True`，且 Provider/模型支持图片 |
| 思考块、供应商签名 | 通过下文的宿主状态扩展保留；基础 Sampling 不支持 |
| 音频、助手图片、自定义消息 | 明确拒绝，不静默删除 |
| 模型选择 | 宿主的 `model` 决定，可传字符串或 `ModelInfo`；服务端偏好不能覆盖它 |
| 输出 token 上限 | 取服务端请求、宿主 `max_tokens` 和宿主 options 中上限的最小值 |
| 温度 | 服务端传入时需要 `allow_temperature=True`；宿主 options 优先 |
| 供应商专用参数 | 只允许宿主 options 提供；服务端 metadata、停止序列和未知参数拒绝 |
| 宿主 options 的工具选择 | 与服务端选择冲突时拒绝请求；更细规则放入 authorize |
| 宿主上下文自动注入、任务增强请求 | 不支持，也不声明 |

`authorize(context, request)` 收到独立副本，只有明确返回 `True` 才批准；它不是修改请求的接口。
可以检查提示词和工具，也可接入预算或人工审阅。模型超时覆盖授权、等待模型并发额度和实际生成。
`max_requests_per_call` 统计同一外层调用中的模型与表单请求总数。服务端 Agent 仍可使用 `RunLimits`。
反向生成不会自动计入外层 Agent 的模型请求预算或 token 用量；标准 Sampling 回复没有 pi-python
的用量信息。需要计费时，使用下文的 `SamplingHandler(observe=...)`。

## 服务端接入

```python
from pi_python import Agent, RunLimits
from pi_python.mcp import SamplingProvider

@server.tool()
async def business(label: str, ctx: Context) -> str:
    async with SamplingProvider(ctx.request_context, timeout=120) as provider:
        agent = Agent(provider=provider, tools=local_tools,
                      limits=RunLimits(max_model_requests=4))
        result = await agent.prompt(label)
        # 使用前检查 result.status，并验证业务结果。
    answer = await ctx.session.elicit_form(
        "选择输出形式", form_schema,
        related_request_id=ctx.request_context.request_id,
    )
    ...
```

显式传入当前请求上下文，不使用全局会话或“当前请求”变量。
不要跨请求保留 Provider，也不要用它启动脱离请求生命周期的后台任务。
退出 `async with` 会撤销 Provider 并取消、等待未完成请求；不会关闭 SDK 会话。
`related_request_id` 使用 SDK 的公开关联机制，包括 HTTP 响应流路由。
通知上下文、现代协议上下文会被拒绝。

`SamplingProvider.stream` 收到完整回复后只产生一个 `ModelEvent.done`，不模拟逐字输出。
模型由宿主选择；服务端 `ModelRequest.model` 不代表有权访问该模型。
标准请求传输 `max_tokens`、`temperature` 和 `tool_choice` 参数；扩展中的 `sampling_profile`
选择见下文。密钥和 Provider 回调不能传输。
工具结果的 `details`、usage、nested_calls 是本地记录，不是模型消息，不会发送。

## 表单结果与失败

界面收到 `ElicitationRequest(context, message, schema)` 和 `CancelToken`。
返回 `ElicitationResponse("accept", content)`、`ElicitationResponse("decline")`
或 `ElicitationResponse("cancel")`。拒绝表示用户明确不同意；取消表示关闭交互。
后两者不能附带回答内容。

第一版接受扁平 JSON 对象中的文本、布尔、数值和单选字符串（`enum` 或带标题的 `oneOf`）。
会验证必填项、文本长度/格式和数值范围。支持 email、URI、date、date-time 格式。
`[mcp-interactive]` 会安装这些格式的校验依赖；缺少对应校验器时会拒绝该格式，不会静默放过。
不接受数组、嵌套对象或 schema 引用。接受结果必须满足约束，不能包含未请求的字段，
也不会把默认值自动解释为用户回答。
服务端提供的 `pattern` 正则约束会在 schema 编译及界面调用前被拒绝。
Python 正则可能阻塞事件循环，异步超时无法中断它；请使用长度、支持的格式或枚举约束。

| 情况 | 协议结果 |
|---|---|
| 用户接受／拒绝／取消 | `ElicitResult.action` 为 `accept`／`decline`／`cancel` |
| 宿主策略拒绝 | 错误 `-1` |
| 模型或人工回调超时 | 错误 `-32001`，不会变成接受 |
| 超过外层调用的回调次数预算 | 错误 `-32000` |
| 请求或回答无效 | 错误 `-32602` |
| 最终 Provider 失败 | `-32002`，附脱敏分类与重试信息，见下文 |
| 界面或其他处理失败 | 不包含异常正文的错误 `-32603` |
| 外层调用取消 | 单次请求取消；仍能回复时返回错误 `-32800` |
| 连接断开 | SDK 连接错误；取消并等待正在运行的回调 |

根据[表单规范](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation)，
表单不能收集密码、API 密钥、访问令牌或支付凭据。
框架拒绝明显的凭据字段名和敏感格式，但不能靠字段名判断所有问题的含义。
宿主界面和策略仍须拒绝敏感问题，显示请求方，并允许用户审阅、修改、拒绝、取消。
用于敏感操作的 URL 模式不在本次支持范围内。

可以通过 `MCPCallbacks(on_event=...)` 接收 `MCPCallbackEvent(context, kind, status)`。
事件不包含提示词或用户答案，context 的 repr 不显示 metadata。
适配器不会默认记录或发给服务端 Provider/界面异常的正文。
审计函数应快速返回；其失败不改变协议结果，清理时最多等待一秒。宿主自身日志由宿主管理。

## 插件授权和就绪检查

宿主通过运行时 API 按 `(插件名, 服务名)` 注入处理器：

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

`mcp.json` 或 `api.add_mcp_server` 的配置可以写
`"required_capabilities": ["sampling.tools", "elicitation.form"]`。
这只是声明需求，不会自动授权。缺少宿主授权时，即使宽松加载也会报错；
运行时授权指向不存在的插件/服务也会报错。回调对象不放进 JSON 配置。
`readiness(mcp_timeout=5)` 会并发 ping 各个会话，再核对要求。ping 失败或超时后，
该服务及其能力不计入本次快照；后续检查成功时，可恢复仍能响应的会话。
`plugins.mcp_capabilities` 反映连接建立或最近一次就绪检查的结果，不是持续监控，
`ReadinessResult.missing_mcp_capabilities` 列出缺失的 `(服务, 能力)`。
已有 call_metadata、严格加载和其他就绪检查继续有效。

## 取消和资源所有权

取消外层工具任务或 `ToolContext.cancel` 会结束该调用的反向工作，并由 SDK 发送取消通知。
Agent 的工具超时和运行超时复用这条取消路径。取消一次调用不会关闭共享会话。
客户端关闭或连接断开时，会取消回调并等待框架创建的任务退出。
Python 无法强制终止阻塞事件循环或无限吞掉取消异常的代码；宿主异步函数必须配合取消。
框架关闭自己消费的异步迭代器，但不关闭借用的 Provider、界面或宿主服务。

stdio 服务进程由 SDK 管理：先关闭 stdin、等待正常退出，必要时升级为强制终止。
POSIX 的强制终止针对进程组；自行 `setsid` 脱离进程组的子进程不在保证内。
未启用下面的进程作用域时，父进程正常退出后，POSIX 下仍存活的子进程不保证被杀。
Windows 使用 SDK 的 Job Object，受系统权限和作业限制影响。
HTTP 客户端不拥有远程服务进程，因此不会终止它。

在 POSIX 系统上，可用 `connect_stdio(command, args, process_scope=True)` 管理一组
本地 MCP 连接的进程。SDK 仍管理直接启动的服务。框架在执行服务代码前登记进程组，
服务内部再使用 pi-python `connect_stdio` 启动的连接会自动继承归属，即使 SDK
为它们建立了新会话。关闭内层连接只清理其子树；关闭外层连接还会清理服务崩溃后
遗留的嵌套进程组。正常结束、异常、握手超时和取消都会执行清理。
框架先发送 SIGTERM，所有残留组共享最多一秒的等待时间，再发送 SIGKILL。
调用方重复取消也不会中断这一步。单条记录损坏或一次信号失败不会跳过其他组；
清理错误会抛出，已有异常时则附加到该异常的说明中。

默认值是 `process_scope=False`，未继承作用域时保留 SDK 原有行为。
作用域内部的连接即使使用默认值，也会归入父作用域。归属信息放在框架私有临时目录中，
通过内部环境变量 `PI_MCP_PROCESS_SCOPE` 传递，不承载工具数据。调用方不应修改这个
变量或临时文件；不需要自行登记进程，也不需要提供业务输出目录。
启动器使用宿主的 Python 解释器，执行原命令并保留其参数、工作目录和配置的环境变量。

这项能力依靠连接间协作，不会扫描并追踪所有系统后代进程。绕过 pi-python stdio
适配器自行脱离进程组的子进程不在范围内。宿主被 SIGKILL 或机器故障时无法执行清理，
操作系统权限也可能阻止发送信号。非 POSIX 系统显式启用时会报 `ConfigurationError`，
默认 Windows SDK 路径不变。POSIX 宿主通过 `MCP_FEATURES` 中的
`"stdio-process-scope-v1"` 标识这项能力。测试覆盖正常退出、强制关闭、嵌套服务崩溃、
取消、启动超时和同级连接隔离。

进程分离和 MCP 都**不是安全沙箱**。
需要共享 Python 对象和状态的可信插件适合进程内调用；需要独立部署、不同语言/运行时，
以及请求内模型和人工交互的工具，可以采用这里的 MCP 接口。

## 真实 Provider、配置选择与计量

`pi_python.mcp.MCP_FEATURES` 是可安装构建的公共能力标识。迁移程序应检查
`{"sampling-host-state-v1", "sampling-profiles-v1", "sampling-metering-v1", "sampling-retry-v1"}`
是否为它的子集。这些能力从 `0.10.0` 开始随正式版本提供；能力检查也能区分此前
仍使用 `0.9.0` 版本号的开发构建。从本仓库安装：
`pip install '.[mcp-interactive]'`；`uv build --wheel` 可生成包含这些能力的 wheel。

### 供应商状态保留

默认的 `SamplingHandler(retain_state=True)` 在初始化时声明实验扩展
`io.pi-python/sampling-v1`。`SamplingProvider` 只在发现该能力后使用扩展。
需要真实 Provider 的服务应使用 `SamplingProvider(context, require_host_state=True)`，
缺少能力会在进入上下文时失败。插件可要求 `sampling.host_state` 和 `sampling.profiles`，
也可以通过 `plugins.readiness(required_mcp_capabilities=...)` 检查。

例如，OpenAI 返回带 `msg_x` 签名的文本和加密推理块。宿主保留完整的
`AssistantMessage`，包括 provider、api、model、消息签名、工具编号、推理状态及响应标识。
服务端收到文本、工具调用和一个随机引用。下一轮，服务端回传该引用，宿主核对可见消息内容
后恢复原始消息，再交给同一配置的 Provider。OpenAI 的 `call_id|item_id` 和 Anthropic 的
工具编号保持不变。供应商签名、推理块和真实用量不通过这个扩展发给服务端。

扩展请求 `_meta["io.pi-python/sampling-v1"]` 的字段为 `conversation`、`profile`、
`history`（按助手消息顺序排列的引用）；回复同名字段为 `{"ref": "..."}`。
服务端 `AssistantMessage.response_id` 保存该不透明引用，**不是供应商 response ID**。
这是供应商状态恢复协议，不是父请求关联协议；关联仍使用 SDK 的 `related_request_id`。
插件业务代码无需读写这些字段。

状态只活到当前外层 MCP 工具调用结束。正常完成、取消、断连和关闭都会取消回调并清空状态。
不同连接、外层任务、SamplingProvider 实例和配置名称的引用不能互用；修改已保留的助手内容
也会被拒绝。不要删除或改写 `response_id`，不要把这些消息持久化后用于另一次工具调用。
同一个 Agent 的后续轮次须保留在同一个 SamplingProvider 上下文中。

这不是直接 Provider 的全部等价实现。服务端只看见可执行的文本和工具调用；仅有推理内容时，
可见文本为空，推理仍在宿主保留。不能在服务端读取思考块、恢复宿主进程重启前的状态，或把
带签名的外部历史导入此作用域。音频、助手图片、命名空间工具、内置服务器工具及延迟结果
仍不支持。Provider 传输目前限定为 SSE；显式 WebSocket、缓存 WebSocket 和自动传输模式
在模型调用前拒绝。完整回复后才产生一个终止事件，没有逐字流式输出。
已用本地 SSE 夹具验证 OpenAI Responses、Anthropic Messages 和兼容 Chat Completions 的
文本、工具轮次及推理重放；这不保证任意模型或网关的私有协议都可用。宿主仍应选择支持的
ModelInfo 和 Provider 参数。未协商扩展时，显式推理配置和已知 Responses Provider 会在
调用模型前拒绝；其他无法表示的回复仍明确失败，不会丢弃签名后继续。

### 可选的增量请求传输

`SamplingHandler(incremental=True)` 在保留宿主状态时默认启用增量传输，
并声明实验能力 `io.pi-python/sampling-delta-v1`。服务端只在宿主声明后使用它。
旧宿主或旧服务端仍走完整请求路径；设置 `incremental=False` 可关闭。
功能检测名称是 `sampling-delta-v1`，插件能力检查名称是 `sampling.delta`。
MCP 协议版本不变，业务代码不需要自行维护缓存。

首次请求发送完整可见历史、系统提示和工具定义。宿主成功返回后，给出可引用的请求编号。
后续只发送变化部分，并引用未变的系统提示和工具定义。例如，首轮已经发送用户任务，
下一轮只需补上模型回答和用户追问。宿主先恢复完整请求，再做格式校验、策略检查、
授权和模型调用。比较时区分 JSON 布尔值与数字。改变或删除工具、改变系统提示、
回到较早历史另起分支，都不会被当成原请求。已有助手消息的完整性校验仍然保留。

协议在原 sampling-v1 元数据中增加 `delta: {}`，表示建立完整请求基准；
之后可发送 `delta: {base, prefix, reuse}`。`prefix` 统计未变的线上消息条数；
`reuse` 只允许引用基准中存在且未变的 `systemPrompt` 和 `tools`。
响应增加 `request_ref`。引用限定在同一次外层工具调用、同一会话和配置内。
并发分支可使用本作用域内任一已确认基准。缺失、失效、越界、跨作用域引用会在调用模型前失败，
不会自动重跑模型。作用域退出也会清理服务端缓存。

宿主会保留请求投影到外层调用结束，并在内部共享未变的消息块和静态字段。
这增加了作用域内缓存内存，包括各次历史列表中的引用；不代表零复制或峰值内存有固定上限。
工具参数、工具定义和原始助手状态仍与 Provider 的修改隔离。可见投影直接跳过隐藏推理和诊断字段，
并缓存可见内容供完整性检查。Provider 收到的仍是完整历史；减少 MCP 字节不等于降低模型 token、
延迟或总分配量，完整转换和校验仍会执行。

### 同一任务选择不同配置

```python
from pi_python.mcp import SamplingHandler, SamplingProfile, SamplingRetryPolicy
from pi_python.providers import HTTPTransport, OpenAIProvider

# 本例把重试集中到 SamplingHandler；避免与 transport 的默认重试叠加。
provider = OpenAIProvider(transport=HTTPTransport(max_retries=0))

async def prepare(context, profile, request):
    request.api_key = await host_credentials.for_task(context.run_id)
    request.on_payload = host_on_payload
    request.on_response = host_on_response
    request.on_provider_stream_event = host_on_stream_event

async def observe(event):
    # run_id / tool_call_id 来自宿主外层调用，request_id 是这次反向请求。
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

`default` 配置来自处理器的 `model/options/tool_choice_format`，不能在 `profiles` 中覆盖。
同一处理器的全部配置共用并发额度。服务端可使用 `SamplingProvider(profile="json")`，
或给某次 `ModelRequest.options` 设置 `sampling_profile="json"`；未知名称被宿主拒绝。
Agent 的主调用、独立 JSON 辅助请求和文本请求可以交替执行，但不要把一个配置产生的有状态
历史交给另一个配置。可直接运行的 `examples/mcp_interactive.py` 展示这三类调用与表单。

配置使用对应 Provider 的公开参数：OpenAI Responses 的 JSON 模式是 `text.format`；
Chat Completions 使用 `sampling_params={"response_format": ...}`，关闭并行工具调用也放在
`sampling_params` 内；Anthropic 使用 `tool_choice_format="anthropic"`，例如
`options={"tool_choice": {"type": "auto", "disable_parallel_tool_use": True}}`。
Anthropic 转换会保留 `disable_parallel_tool_use`。无工具的请求会移除工具选择和并行工具参数，
包括 `sampling_params` 中对应的字段。JSON 内容是否满足业务 schema 仍由业务验证。

`prepare(context, profile, request)` 是宿主侧的可修改请求接口，在 `authorize` 之前执行，
可以注入请求级凭据和三个 Provider 回调。它属于可信宿主代码，可覆盖配置；不要把它交给插件
控制。`authorize` 仍只收到副本并返回布尔授权。服务端不能传入凭据、回调或任意供应商参数。
不要同时用自定义 Provider 包装器和 prepare 重复安装同一回调。

### 计量、错误和重试的责任

`observe(SamplingObservation)` 在每次 Provider 流结束后、重试或 MCP 转换前调用一次。
它包含调用归属、配置名、尝试编号、状态、真实供应商响应编号，以及原样复制的 `usage`。
缓存读写字段保留。缺少用量时为 `None`，不是零；三类实际 Provider 在上游没有返回用量时
通过 `diagnostics` 中的 `{"type": "usage_unavailable"}` 标明未知。为保持直接 Provider
调用兼容，原有归一化用量字段仍保留；处理器读取该标志，不会将默认零计为实测值。标准 MCP 回复仍没有用量，服务端的空字典不可用于计费。
在嵌套连接中，只在实际模型所在的根宿主计量；中继 SamplingProvider 没有真实用量。
Chat Completions 的空或不完整用量对象也视为未知。`prompt_tokens` 和 `completion_tokens`
必须同时是有限、非负数值；明确返回的零仍是有效测量。末尾空用量不会覆盖已有完整计数，
顶层用量为空时仍可读取 choice 内的有效计数。

观察函数是计量接入点，不是默认日志。它应及时写入宿主的计量系统；异常会使本次请求失败，
但不会重试已经完成的模型响应。进程崩溃下的持久化与去重由宿主负责。
用于去重的键应包含宿主运行标识、外层工具编号、反向请求编号和 attempt，不能只用供应商 ID。
如果需要计量已损坏或未结束流的部分消耗，宿主应通过原始 Provider 事件回调收集；框架不会
根据不完整流猜测用量。借出的 Provider 始终由宿主关闭。

`SamplingRetryPolicy` 默认只尝试一次。可选重试仅针对结构化 HTTP 状态
429、500、502、503、504；认证、请求错误、未知错误和取消不重试。
框架不检查原始异常字符串中的数字。等待采用指数退避，`max_delay` 限制本地退避；服务给出的
`Retry-After` 更长时不会缩短它，总体模型超时仍可终止等待。取消也能中断等待。
整个重试期间只重复当前模型请求，已完成的业务工具不重放。

已有 `HTTPTransport` 默认包含有界 HTTP 重试。可以保留它，并让处理器保持一次尝试；
也可以按上面的例子禁用 transport 重试，由处理器负责。不要在两层同时扩大重试次数。
观察事件的 attempt 是 Provider 流尝试次数；transport 内部拒绝的 HTTP 请求可通过
`on_response` 查看，通常没有模型用量。业务服务不要再自动重试整个外层工具。

最终模型失败返回 MCP 错误 `-32002`，`data` 包含脱敏的 `category`（`rate_limit`、
`server`、`authentication`、`request` 或 `provider`）、`retryable`、`retryAfter`、`attempts`
和 `retryOwner="host"`。`retryable` 描述底层错误类别，不授权重放业务工具。
直接调用处理器得到 `SamplingFailure`；MCP 服务端可检查 SDK `MCPError.data`。
授权拒绝、超时、取消继续使用前文的独立错误码。异常正文、密钥、提示词不写入默认日志。
