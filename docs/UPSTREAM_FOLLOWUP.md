# Pi 上游跟进与应用迁移（0.12.0，2026-10-28）

本轮选择性跟进 Pi v1.1–v1.4 的修复及功能，并纳入两项有明确来源的 main 修复。核心循环继续保留 v1.0.0 差分基线；这不是完整移植 v1.4.0，也没有改变 pi-python 的库定位。本页对应 0.12.0，升级步骤见[发布说明](releases/0.12.0.md)。

后续 [接口审查与实施](UPSTREAM_INTERFACES.md) 已纠正计时和流等待，并按用户后续批准加入 `pi_python.core` 标准合同入口及共享请求配置修正。研究入口的便利能力继续保留，迁移见 [双入口指南](zh/CORE_AND_RESEARCH.md)。接口探针分别记录 core 对齐情况与研究入口的有意差异，不能将这些结果解读为整个 Pi 的完整等价性。

## 已实施的变化

| 范围 | 当前行为 | 上层应用需要检查什么 |
|---|---|---|
| OAuth 刷新 | 刷新开始后，取消等待令牌轮换及写回完成；网络刷新单独限时 15 秒；写回失败保留 dirty 状态供下次重试 | 取消不再立即中断刷新；持久化回调必须能结束。仍不支持多个进程共享同一刷新锁 |
| 恢复判断 | 扩展暂时过载、断流、容量不足、HTTP 413 和 LiteLLM 上下文超限判断；Business 工作区用量限制不再建议重试，HTTP transport 也不重试此类 429 | 应用若使用这些分类器，会得到新的判断；core 没有增加自动压缩或模型流重试 |
| 消息转换 | 跨模型 reasoning 及签名从请求中省略；纯空白工具结果使用占位文本；Anthropic 错误工具结果中的图片移到全部工具结果之后 | 旧 reasoning 不再以普通文本暴露给新模型。存储的原始历史不改写 |
| 工具及强度变更 | Anthropic 使用 inline-tools beta 和完整内联定义；支持同名工具替换。Responses 初始及增量工具用 `additional_tools`，保留删除/重新加入的解释。支持的 OpenAI API 模型使用 `configuration_update` | 请求形状和缓存前缀发生变化；请求审计器、代理白名单及快照测试需要更新。旧 `resolve_transcript_tools(messages, bool)` 公开合同保留 |
| 空回答后的系统指令 | Anthropic 暂存的系统变更等待下一条非空 assistant，不再被空回答提前释放 | 请求顺序变化；原始历史不变 |
| 模板参数 | 空引号 `""` / `''` 保留为空参数，占据原来的位置 | 依赖旧错误移位行为的模板应修正 |
| 模型目录及估算 | 固定 npm v1.4.0 发布目录，共 68 条（Claude 17、OpenAI 42、Codex 7、DeepSeek 2）；字符估算从每 token 4 改为 3.5 个 UTF-16 单位 | Codex 不再内置 `gpt-5.5`、`gpt-5.3-codex-spark`。可选支持的模型或显式提供 `ModelInfo`。估算更保守，部分长请求输出上限会缩小 |
| 默认耗时 | assistant、工具结果、工具 outcome 和结束事件默认提供可测得的 `duration_ms` | 见下方结构迁移；不是 opt-in 功能 |
| 请求身份 | `Agent(identity=...)` 及 `options["identity"]` 控制 User-Agent 和供应商归属头 | 默认 User-Agent 增加版本/运行时/平台；显式身份 headers 优先。默认不将本库请求归属于官方 Pi |

上游公开目录没有本轮检查到的 `gpt-6.1-terra` 记录，因此没有推测它的上下文、价格或能力。DeepSeek 两条 Responses 记录保留本项目既有适配，不能宣称与上游 DeepSeek Chat Completions 路径一致。

## 耗时与历史迁移

用户已明确选择“跟随上游默认输出，接受结构迁移”。需要同步升级消费事件、消息和 `dataclasses.asdict()` 结果的应用。

- 写出消息 schema **4**、事件 schema **2**；新版读取器仍接受旧消息 schema **3** 和事件 schema **1**。旧版读取器不能读取新版输出，不提供自动降级写出。
- `AssistantMessage.duration_ms` 计模型流的毫秒数，包括其中的请求准备、网络及传输重试；Agent 的上下文转换钩子不在模型流计时内。已有耗时保持不变，早于本次流的历史消息不补写。本次新生成的 deferred 回答可计时，但该时间不是后台任务完成耗时。内置 Provider 的 assistant timestamp 表示请求开始时间，仍使用 Python 的 Unix 秒。
- `ToolOutcome.duration_ms`、`ToolResultMessage.duration_ms`、`tool_execution_end.data["duration_ms"]` 只计工具执行；不计参数准备、前后钩子和结果整理。尚未执行或未停止的调用没有耗时。
- JSON 消息/事件编码省略未测得的耗时；dataclass 的 `asdict()` 会包含 `duration_ms=None`。字段值是非负整数，单位毫秒。`0` 是有效测量，不代表未执行。
- `execute_tools()` 的 application batch 历史为已执行调用保存耗时。默认转换给模型时省略耗时；自定义转换器自行决定如何处理。
- `include_tool_origin` 仍只控制 origin/source 事件字段，不能关闭耗时字段。

应用迁移顺序：先允许新 schema 和可选 `duration_ms`，再部署新库写入端，最后检查监控、数据库列、事件重放及严格 JSON/Pydantic 解析。测试包含旧严格订阅者在 `tool_execution_end` 拒绝新字段的例子：工具可能已经完成副作用，随后订阅失败使 run 失败。因此不能将订阅失败简单理解为工具未执行。

可以用 `require_features(["default-duration-ms-v1", "client-identity-v1"])` 检查当前构建。来源属性仍不写入 ToolContext/ToolOutcome 的 dataclass 字段；本轮新字段是耗时。

## 身份和协议范围

`identity` 是 JSON 对象，支持 `user_agent`（`short` / `detailed` / `{"custom": "..."}`）、`version`、`attribution`（name、url、可选 categories）。Agent 默认值在 run 开始时复制，请求 options 按字段覆盖；`attribution=None` 关闭继承归属。显式身份 headers 还可覆盖或移除生成值，包括 HTTP 客户端自动补入的 User-Agent。

默认品牌使用 `pi-python`，默认不发送 attribution；这是独立 Python 库的有意差异。Anthropic OAuth 保留其 Claude CLI User-Agent，除非应用显式覆盖 header。OpenRouter 端点按主机名匹配，避免把归属头发给名称仅包含该字符串的其他域。`ProxyProvider` 按固定上游代理协议处理，不转发 identity。后续接口实施已增加 model-level headers 与请求覆盖规则；动态目录刷新和供应商专有接口仍未新增。

## 来源与复现

主参照为 [Pi v1.4.0 / 4a080252](https://github.com/earendil-works/pi/tree/4a080252f06caf63a525d7873c5d94d3c23887e0)。模型目录使用 npm 发布包，保存 SHA-512；源码归档和相关文件保存 SHA-256，见 [source-manifest.json](../reference/followup/source-manifest.json)。固定重建不会重新拉取浮动目录。

| 改动依据 | 固定来源 |
|---|---|
| 耗时字段 | [36a686ee](https://github.com/earendil-works/pi/commit/36a686ee8dd73afe8242011db387040890e24093) |
| 客户端身份 | [adc5fa9f](https://github.com/earendil-works/pi/commit/adc5fa9f800bee587e35b1a7b316e4b11a8aef24) |
| 跨模型 reasoning | [91130942](https://github.com/earendil-works/pi/commit/911309429175254550837167baee245d5f9dbcb6) |
| 工具空白结果 | [4dd2b149](https://github.com/earendil-works/pi/commit/4dd2b149d155d7369de16ea4dc77c84e9f5ffb24) |
| Anthropic 错误图片 | [5ab7fe74](https://github.com/earendil-works/pi/commit/5ab7fe74c400097b386662205cd93900695b8a71) |
| Responses 工具缓存 | [caa6967b](https://github.com/earendil-works/pi/commit/caa6967b62122bad12e4a2af287932b9e881bdf0) |
| Responses 原生强度配置 | [60ac3bcc](https://github.com/earendil-works/pi/commit/60ac3bcc3b8d6df34a44c0ed677a9eb9c74e52ac) |
| LiteLLM 上下文超限（main） | [d3398f82](https://github.com/earendil-works/pi/commit/d3398f826ae2654b52a7988830d533a354754c13) |
| 空回答后保留系统更新（main） | [3bc6eeba](https://github.com/earendil-works/pi/commit/3bc6eebacf3a6497eb82a66e02ca8644cfa3dde2) |

```bash
uv sync --locked --extra providers --extra mcp-interactive
uv run python scripts/bootstrap_reference.py
uv run python scripts/bootstrap_followup_reference.py
uv run python scripts/verify.py
uv run python scripts/linux_verify.py
uv run python scripts/benchmark_partial_json.py
```

core 差分仍使用 v1.0.0 的 25 组行为样例，排除不可确定的耗时值，耗时另有边界测试。Provider 差分包含 58 组，WebSocket 2 组，恢复判断 55 条；插件样例增加空引号参数。比较结果由真实固定上游运行生成，不能手写 expected 代替。新增 API、取消、MCP 历史保留、严格应用解析和请求身份另由 Python 测试覆盖。实际命令、退出码和输出由 [verification.json](../compat/results/verification.json) 记录；Linux 记录单独生成。

本轮没有使用真实账户调用模型或消费刷新令牌，也没有验证外部应用的实际数据库、代理或部署。历史版本的 live 测试不是本轮新协议的验证证据。通过库内及离线协议测试不能保证所有上层应用兼容。

## 已评估但未纳入的 main 变化

[增量 JSON 解析器 f715a15c](https://github.com/earendil-works/pi/commit/f715a15c101ca8a44d759bceeb7ec2a51024c81c) 涉及新的容错解析器、所有 Provider 的流状态和不可变快照。本地基准确认当前预览解析有重复扫描成本：固定 256 字符分片的单字符串参数，4,096 / 16,384 / 65,536 字符测试的三次耗时及中位数见 [原始记录](../compat/results/partial-json-baseline.json)。该测试只测 Python 预览函数，不含传输、事件复制或 UI，也没有测量上游速度。

本轮完成性能评估，保留现有解析器；后续应单独移植增量状态并核对截断字符串、转义、数值、非法最终 JSON 和快照不可变性。现有最终参数仍严格解析，预览不用于执行。

Anthropic 顶层 effort 从 high 改 low 的 [main 改动 e3bc05e8](https://github.com/earendil-works/pi/commit/e3bc05e8719ac939c364a2236807376108107afa) 未纳入；这里保留 v1.4.0 行为。没有将全部 main 变动等同于稳定发布，也没有新增自动压缩、durable 调度或 UI 层功能。
