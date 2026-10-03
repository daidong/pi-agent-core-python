# 验收映射

首期测试证明的是本文列出的文本和工具行为，不是完整 Pi 或模型智能。27 行是设计中的用例组，不是 27 个单独测试。具体测试总数与命令见 [验证记录](results/verification.json)，逐 fixture 结果见 [差分记录](results/conformance.json)。

| 用例组 | Python 行为证据 | 上游证据 / 分类 |
|---|---|---|
| C01 文本与增量 | test_C01_stream_and_end | C01-text，mapped；增量一致性另作 D7 合同 |
| C02 工具结果进入请求 | test_C02_result_in_next_request_and_private_details | C02-tool，mapped |
| C03 串行顺序 | test_C03_C05_serial_batch | C03-sequential，mapped |
| C04 并发结束与提交顺序 | 两个 test_C04 测试，分别屏障控制执行与后置钩子 | C04-parallel，mapped |
| C05 任一串行影响整批 | test_C03_C05_serial_batch | C05-one-sequential，mapped |
| C06 普通失败保留同批成功 | test_C06_failure_does_not_cancel_sibling | C06-one-failure，mapped |
| C07 未知工具和非法参数 | test_C07_C08_D1_D2 与 schema 拒绝测试 | C07-unknown，mapped；严格验证为 D1 |
| C08 先准备再验证 | test_C07_C08_D1_D2 | C08-prepare，mapped |
| C09 阻止、替换和清除结构化结果 | test_C09_block_and_clear_structure；执行前钩子抛错见 test_failing_before_hook_is_a_tool_error_not_a_rejection | C09-block / C09-replace / C09-before-error，mapped；上游独立结构化替换测试通过 |
| C10 长度截断零执行 | test_C10_truncation_all_tools_fail_without_execution | C10-length，mapped |
| C11 transform 先于 convert | test_C11_transform_before_convert_defensive_copies | C11-transform，mapped；复制隔离为 D5 |
| C12 系统与工具声明重放 | test_C12_system_replay / runtime_tool_declarations_track_config | C12-system，mapped；上游 transcript-tool-changes 测试通过 |
| C13 指导的注入时点 | test_C13_C14_queues_and_modes | C13-steering，mapped |
| C14 后续队列与消费模式 | test_C13_C14_queues_and_modes | C14-followup-one / all，mapped |
| C15 准备钩子与配置持续 | 两个 test_C15 测试 | C15-prepare，mapped |
| C16 停止与继续 | 参数化 test_C16 与错误结束测试 | C16-mixed / terminate / continue / end，mapped |
| C17 重复请求和继续边界 | test_C17_busy_and_continue / continue_queued_final... | 上游 agent.test 中 prompt/continue 用例通过；Python API 映射测试 |
| C18 订阅后空闲和故障 | 三个 test_C18、结束订阅取消测试 | 上游 agent.test 的异步 listener/idle 用例通过；故障诊断为 D7 |
| C19 两种取消语义 | 参数化 test_C19、调度前取消测试、test_C20_abort_mid_stream_finishes_the_turn_with_partial_content | 显式 abort：C19-abort-stream / C19-abort-tools，mapped；外部 Task 取消为 D4 |
| C20 已完成保留、取消不锁定、显式未知不重放 | test_C20_abort_running_tool_is_an_error_result_and_agent_stays_usable、test_C20_explicit_unknown_stops_current_run 和后置阶段取消测试 | 取消运行中工具：C19-abort-tools，mapped；显式未知为 D4 |
| C21 超时、清理和上限 | 六个 test_C21（含默认不设上限）、延迟成功不得覆盖超时测试 | intentional_difference，D3/D4 |
| C22 原始执行和整理失败 | 参数化 test_C22 | intentional_difference，D6/D7 |
| C23 配置隔离和环境 | test_C15_C23 / test_C23_no_implicit_environment_configuration | intentional_difference，D5；运行更新持续另在 C15 差分 |
| C24 损坏流和能力拒绝 | 参数化 test_C24、尾部异常测试、两组 Provider 中途失败测试 | 供应商中途出错：C24-provider-error，mapped；损坏流拒绝为 D7/D8 |
| C25 JSON、版本、导入 | 两个 test_C25、事件编解码 | Python 合同；没有持久化或导入后自动执行 |
| C26 release 候选 | test_C26_noop_duplicate_and_synthetic_upgrade 及参数化拒绝测试 | 合成接口演练；独立远端校验脚本另存实际记录 |
| C27 wheel 安装 | Linux 容器内断网安装、示例、全部 pytest | 实际组合见 linux-3.11/3.12/3.13.json |

## 有意差异与范围外行为

D1–D8 保持原设计的含义。D1 严格输入；D2 执行前钩子不能通过原地修改参数改变调用；D3 资源预算；D4 Python 取消及未知结果；D5 状态复制；D6 输出检查；D7 诊断、原始结果和完整流校验；D8 文本与工具子集。每项有独立 Python 断言，不以 skip 代替验收。

范围外项目：真实模型 SDK、图片/音频/专有推理、OAuth、MCP 客户端、TUI/CLI、持久化与崩溃恢复、子 agent 管理器、Slurm/PBS，以及真实 HPC/科研任务效果。模型侧不支持的内容显式拒绝。上游真实模型 e2e、代理和其他应用测试未运行；它们不计入通过数。

## 已知验收边界

共享对照是 25 个脚本输入（0.5 时为 21 个）。上游 97 项选定单元测试证明参照可运行；它们不等价于 97 个 Python 差分输入。完整上游 API 和未覆盖输入没有兼容保证。无待解释的共享 fixture 差异；后续增加行为时，应扩充 fixture 再扩大声明。

## 0.2 范围修订

上述“首期范围外”是 0.1 的历史边界。根据用户追加要求，0.2 已将图片、供应商推理、Claude/OpenAI Provider、OAuth、streamProxy、独立循环和便利 API 纳入实现，D8 相应扩大。D1–D7 保持。

新增 Python 合同见 `test_extended_core.py`、`test_providers.py`、`test_oauth.py`。8 个共享网络流输入在 `provider-fixtures/`，实际上游解析器输出和 Python 输出保存在 [provider-conformance.json](results/provider-conformance.json)。它们对比最终内容、签名和停止原因，不等价于所有供应商请求选项或真实账号端到端兼容。

当前边界与明确差异见 [PROVIDERS.md](../docs/PROVIDERS.md)。0.1 的 accepted baseline 保留为历史核心验收记录，新增能力单独记录，不修改固定上游提交。

## 0.3 范围修订

当前数据合同使用 schema 3，支持完整文本/推理/工具块事件、模型诊断与响应元数据、工具 details/usage/nested_calls、系统文本块和完整工具钩子上下文。新增测试见 `test_contract_v3.py`。D8 再次扩大；D1 严格校验、D5 状态副本、D4/D7 未知结果不重放继续保留，不能列为缺失功能。

Provider 差分从 8 组扩大到 22 组，增加完整请求体、语义请求头和事件顺序/载荷，包含 Codex、签名重放、图片、工具 schema、缓存与推理参数。`test_models.py` 验证能力表，`test_recovery.py` 验证重试边界、取消、握手回退、账号隔离、连接复用和重复响应头。真实联调及未覆盖项以 [当前实施报告](../docs/IMPLEMENTATION.md) 为准；上面的 0.1/0.2 范围描述仅作历史记录。

## 0.4 范围修订

请求转换改为逐项移植上游 `buildParams`、`convertMessages`、`convertResponsesMessages` 和 Codex 请求体构造，并按模型表能力原位发送会话中途的系统指令、工具增删和推理强度。新增 12 组 Provider 差分使用真实模型记录和上游 `streamSimple` 入口，旧 22 组保持通过；新增 2 组 WebSocket 多轮差分对照增量请求和一次性重试。Python 合同见 `test_session_changes.py` 与 `test_websocket_continuation.py`。上游 `anthropic-mid-conversation-effort` 与 `openai-codex-stream` 测试加入参照验证。

D1–D8 不变。本轮明确记录的差异：WebSocket 帧不带 `stream` 字段；请求发出后的 WebSocket 故障不回退 SSE；同一会话的并发请求排队而不是另开一次性连接；Claude 推理预算不足 1024 时报配置错误而不是发送 1024；直接 OpenAI API 的 WebSocket 不使用增量请求（上游无此路径）。

## 0.5 结构修订

0.5 不改变执行行为，只减少并存的写法：Provider 事件只保留与上游相同的一套（`done` 取代 `final`，工具参数增量按块位置），所有 Provider 的事件经同一个检查器；模型能力只来自 `ModelInfo`，表外模型名在真实 Provider 中报错。旧的 20 组 Provider 输入改走上游 `streamSimple` 入口并使用与运行器相同的 `test-model` 记录，与 Agent 的实际调用路径一致；两组重放输入的历史回答补上了上游必需的 `usage` 字段。`agent.py` 拆为会话（Agent）、一次运行（Run）和输入队列三部分。核心 21 组、Provider 34 组、WebSocket 2 组差分保持通过。

## 0.6 修订：失败与取消对齐 Pi

检查发现三处没有写进文档的偏差，现已按上游改正，并各补一组差分：

| 情况 | 改正前 | 现在（与上游相同） | 差分 |
|---|---|---|---|
| 模型流中途出错 | 换成一条空的失败消息，供应商和模型名是 `mock`，已流出的内容和用量丢失 | 提交供应商给的失败消息，保留部分内容、身份和错误文字；`message_start` 只发一次 | C24-provider-error |
| 模型流中途被取消 | 不调用 `finish_turn`、不发 `turn_end`、不留回答 | 记一条 aborted 回答，照常调用 `finish_turn` 并发出 `turn_end` | C19-abort-stream |
| `before_tool_call` 抛异常 | 整个运行失败 | 该调用得到错误结果，模型继续 | C09-before-error |

同时改了两项设计选择：
- 取消运行中的工具按 Pi 处理：工具得到 `Operation aborted` 错误结果，批次收尾后再记一条不发请求的 aborted 回答，Agent 仍可使用（差分 C19-abort-tools）。工具超时同样记为错误结果，运行继续。只有工具主动抛 `ToolOutcomeUnknownError` 才停止运行并锁定实例。
- `RunLimits` 的模型请求数、工具调用数和工具并发数默认都不设上限，由应用设置；同一批工具默认全部同时执行，与上游的 `Promise.all` 相同。

为此扩展了两个运行器：响应可以先流出文本再失败或等待取消，操作可以是 `abort`，工具可以等到取消，执行前钩子可以抛错，`finish_turn` 调用被记录。比较规则新增一条映射：流式回答开始时的 `message_start` 不比较结束原因（上游此时为 `stop`，Python 为 `pending`）；流中的部分快照不比较，只比较提交的消息。旧的 21 组输入未改，只有两组带 `finish_turn` 的结果多记录了这个钩子。

仍保留的取消差异，都在 Python 边界上：
- 外部取消 `prompt` 的 Task 时立即退出并重新抛 `CancelledError`，不追加 aborted 回答。
- 取消送达后最多等清理期限（默认 1 秒）；逾期仍在运行的工具得到 `not_stopped` 结果，运行立即停止，实例不可再用。上游会一直等待工具结束。
- 串行批次或准备阶段被取消时，上游不给尚未准备的调用产生结果；本库为每个调用都产生 `Operation aborted` 结果，保持历史中没有悬空调用。
- 被取消的工具不调用 `after_tool_call`；上游会带着错误结果调用它。

## 0.7 修订：易用性与通用性

这一轮的目标是好装、多版本可用、容易在上面搭完整的 agent。按与上游的关系分三类记录。

与上游一致，并有对照证据：
- **Chat Completions 接入**：逐项移植 `openai-completions.ts`，新增 16 组 Provider 差分（Provider 差分共 50 组），覆盖 Ollama、vLLM、llama.cpp、OpenRouter、DeepSeek 等兼容配置和全部 11 种推理参数格式。与上游的差异见 [PROVIDERS](../docs/PROVIDERS.md#本地模型与-openai-兼容服务)。
- **出错判断**：`is_context_overflow`、`is_retryable_error`、`is_recoverable_length` 移植自 `overflow.ts` 和 `retry.ts`，44 条共享样例与上游函数的实际输出一致（`scripts/recovery_conformance.py`）。另外认识本库自己的错误格式：Python 网络错误名，以及按 HTTP 状态码判断本库的 HTTP 错误；上游格式的文字仍按上游规则判断。
- **工具 schema**：不再只接受一个子集。与上游一样接受 draft-07 等标准 schema，并按 `format`、`pattern` 校验参数（已用上游校验器对同一 schema 实测）；仍只允许指向 schema 内部的引用。D1 的"不转换类型"不变。
- **HTTP 错误正文**：与上游一样保留，最多 4000 字符；本库另外把请求所带的凭据替换为 `[redacted]`。

Python 新增，上游核心没有对应：
- `@tool` 从带类型标注的函数生成工具；普通函数在工作线程运行；工具返回值自动转换。
- `prompt_sync`、`continue_run_sync`、`run_sync`：共享后台事件循环的阻塞调用。
- 最后一条回答失败或被取消时，`continue_run()` 直接重试。上游核心在这种情况下拒绝继续，Pi 的应用层先删掉失败回答再继续；本库保留失败回答，重放时跳过它。
- MCP 适配 `pi_python.mcp`。上游把 MCP 客户端放在独立包 pi-mcp，包装成工具的做法写在示例里；本库作为可选依赖 `[mcp]` 提供，内容转换规则与上游示例相同。
- `abort()` 可在任何线程调用；超过清理期限仍在运行的工具结束后，实例自动恢复可用；没有配置 Provider 时直接报错。

已知差异：
- 准备钩子抛错时，事件里会出现一个没有对应 `turn_start` 的 `turn_end`。上游 `handleRunFailure` 也是这样，保持一致。
- Python 和 JavaScript 的正则方言有细微差别，个别 `pattern` 的判断可能不同。

验证（0.7.0 发布时）：Python 测试 308 项；核心差分 25 组、Provider 差分 50 组、WebSocket 差分 2 组、出错判断 44 条，全部与上游一致。Docker 内 Python 3.11–3.14 断网安装 wheel，各 304 项通过（4 项 MCP 测试因锁文件不含 MCP SDK 而跳过）；本机安装后 3.14t 308 项、PyPy 3.11 304 项通过。依赖取最低版本（jsonschema 4.18、httpx 0.27、websockets 14.2、PyJWT 2.8）和最新版本都通过；MCP 适配在 SDK 1.10（它要求 jsonschema 4.20 以上）和 2.3 上都测过。macOS 和 Windows 只写进了 CI 配置，还没有实际运行；本机也没有运行中的本地模型服务，Chat Completions 接入尚未连接真实服务器。
