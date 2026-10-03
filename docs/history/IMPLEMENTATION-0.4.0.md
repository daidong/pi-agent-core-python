# 实施与验证结果（0.4.0 存档）

这一轮检查的是：长会话中途增减工具、追加系统指令或改变推理强度时，Python 版发给 Claude 和 GPT 的请求是否与固定的 Pi `v1.0.0` 相同，以及 Codex 复用连接时能否只发送新增内容。0.3 在 12 个使用真实模型记录的场景里没有一个与上游相同；0.4 全部相同，并已用本地 Claude、Codex 订阅实际跑通。应用现在可以在会话中途调整工具和推理强度，请求开头保持不变，服务端缓存不必重建（本轮核对了请求结构，没有测量实际命中率）。下一步要验证登录刷新，需要你本人做一次新登录。

## 要解决的问题

一次对话会被反复发送给模型。服务端会缓存请求开头相同的部分（前缀缓存），开头一变，缓存就失效，费用和延迟都上升。0.3 把所有系统指令合并成开头一条，把工具列表换成当前集合；所以会话中途加一个工具，就会改写请求开头。

较新的模型允许把这类变化写在发生的位置。模型表记录了哪些模型支持：Claude Opus 5、Opus 5.5、Sonnet 5.5、Fable 系列和 Opus 4.8，GPT-5.4 起的多数 OpenAI 模型（nano 和部分 pro 型号除外），以及 Codex 的 GPT-5.5 起各型号。上游 Pi 对这些模型原位发送变化，对其他模型才合并。

举一个联调中实际发生的例子。智能体开始时只有加法工具，先算了 2+3；随后应用加入乘法工具，追加一条"结尾写 DONE"的指令，并把推理强度从低调到中。0.4 发给 Claude Sonnet 5.5 的第三个请求里：

- 请求开头声明的工具列表仍以加法工具开头，后面是一个永不可用的占位工具和标为"延迟加载"的乘法工具，开头部分与第一个请求完全相同；
- 新指令和"启用乘法工具"的标记作为一条系统消息，放在最新的用户消息之后；
- 每条历史回答前保留它当时的推理强度（低），末尾追加本次强度（中）。

模型随后调用了乘法工具，回答以 DONE 结尾。Codex 的同一流程在一条连接上完成四个请求，后三个只发送新增内容和上一回答的编号（`previous_response_id`）。

OpenAI 和 Codex 只能原位表达新增工具；删除工具时与上游一样发送完整的当前工具列表，请求开头会变。Claude 的增减都能原位表达。

## 本轮改了什么

| 内容 | 对应上游 | 结果 |
|---|---|---|
| 会话中途的系统指令、工具增删、推理强度 | Claude Messages 与 OpenAI/Codex Responses 的请求构造 | 按模型能力原位发送；无法表达时退回合并，与上游规则相同 |
| Claude 请求中模型相关的细节 | 同上 | 推理文字请求为摘要形式、关闭推理时显式关闭、对应的 beta 头、服务端备用模型、输出上限按上下文估算截短 |
| OpenAI 请求中模型相关的细节 | 同上 | 默认输出上限、显式缓存模式、ChatGPT 登录令牌不发送服务端拒绝的字段、非推理模型使用 system 角色 |
| 历史重放 | 上游消息变换 | 跳过失败的回答；跨模型推理转为文本；工具调用与结果之间的系统消息移到结果之后 |
| Codex 连接复用 | Codex WebSocket 传输 | 增量请求；上一回答丢失或连接数已满时换新连接完整重试一次；空闲 5 分钟或满 55 分钟后重建连接 |

最后一项还修了 0.3 的一个缺陷：服务端关闭空闲连接后，0.3 仍在旧连接上发送，请求直接失败；0.4 先检查连接状态再复用。

有几处行为对已有代码可见。`provider_thinking_level` 现在只在使用强度标记的 Claude 模型上记录，需要请求等级时应读 `thinking_level`。Claude 开启推理时会返回推理摘要文字。由多个文本块组成的系统消息改用单个换行连接，这是 0.3 与上游不一致的地方。`auto` 传输在有会话编号时也复用连接，与上游相同。详细说明见 [API](../API.md#04-会话变更与上下文估算) 和 [模型接入](../PROVIDERS.md#会话中途的变更)。

## 验证结果

| 要回答的问题 | 证据 |
|---|---|
| 真实模型的请求与上游是否相同？ | 新增 12 组真实模型输入，Python 与固定上游的请求体、语义请求头、事件和结果全部相同；同一批输入下 0.3 为 0/12 |
| 旧行为是否回归？ | 原有 22 组 Provider 输入和 21 组核心输入仍与上游相同；Python 测试 226 项通过（0.3 为 212 项） |
| 增量请求是否与上游一致？ | 2 个多轮场景中，每一帧的内容、所用连接和停止原因都与上游相同；唯一差别是 Python 的帧不带 `stream` 字段 |
| 参照本身是否可靠？ | 上游 147 项相关测试在固定版本通过；新移植的 5 个上游源文件与 GitHub 固定提交逐字节一致 |
| 真实服务是否接受新请求？ | Claude Sonnet 5.5 订阅：4 个请求均返回 200，模型调用了中途加入的工具。Codex GPT-6 Luna：1 条连接、4 个请求、3 个增量请求，模型调用了中途加入的工具 |
| 重写后旧功能是否仍可用？ | 0.3 的实测矩阵在三个接入上重跑，文本、工具、推理及签名重放 9 项全部通过 |
| 能否在 Linux 安装？ | Python 3.11、3.12、3.13 容器内断网安装构建好的 wheel，示例和全部测试通过 |

重跑实测时发现一个单元测试没有覆盖的缺陷：DeepSeek 的请求构造函数没有跟上新参数，所有 DeepSeek 请求在本地就失败，没有发出。修复后补了一项直接走 DeepSeek 流程的回归测试，再次实测三项通过。失败记录保留在附录。

Claude 的推理题答案正确，但仍没有遵守"只输出数字"，与 0.3 相同；这一项按答案正确计为通过。

## 仍未覆盖的部分

登录和令牌刷新没有做账号侧验证。现有实测只读取其他客户端（Claude Code、Codex CLI）的令牌而不刷新，因为刷新会让那些客户端的令牌失效。验证刷新需要本库自己登录得到的凭据，需要你在浏览器里完成一次登录。OpenAI API key 和 ChatGPT 直接授权本机没有可用凭据，也没有实测；后者不发送哪些字段的规则来自上游，只做了本地测试。

供应商约束采样（OpenAI 的语法约束工具和严格 JSON schema 工具）、后台任务轮询、音视频生成、模型目录自动更新，以及 MCP、界面、会话持久化等应用层功能仍未实现。WebSocket 有几处保守差异：请求发出后的断线不改走 SSE 重发；同一会话的并发请求排队，不另开连接；直接调用 OpenAI API 的 WebSocket 不使用增量请求，因为上游没有这条路径。这些都记在验收映射中。

建议下一轮先做登录刷新：你运行一次 `examples/provider_chat.py --login`，把凭据存到专用文件，我再验证到期前刷新和并发刷新。约束采样等供应商专有功能，按应用实际需要再加。

## 复现与验证附录

- 实现说明：[API](../API.md)、[模型接入与会话变更](../PROVIDERS.md)、[验收映射](../../compat/COVERAGE.md)
- 全部检查命令与输出：[verification.json](../../compat/results/v0.4.0/verification.json)，命令 `uv run python scripts/verify.py`
- 差分结果：[核心](../../compat/results/conformance.json)、[Provider](../../compat/results/provider-conformance.json)、[WebSocket 多轮](../../compat/results/websocket-conformance.json)；输入由 `scripts/make_provider_fixtures.py` 生成，WebSocket 参照运行器为 `reference/websocket-runner.ts`
- 0.3 在新输入上的结果：[against-0.4-fixtures.json](../../compat/results/v0.3.0/against-0.4-fixtures.json)
- 实测（`scripts/live_providers.py`，不进入 CI，不保存凭据或模型正文）：[Claude 会话变更](../../compat/results/live-session-claude.json)、[Codex 会话变更与增量](../../compat/results/live-session-codex.json)、[Claude 与 Codex 回归](../../compat/results/live-regression-0.4.json)、[DeepSeek 回归](../../compat/results/live-regression-0.4-deepseek.json)、[首次重跑，含 DeepSeek 本地失败](../../compat/results/live-regression-0.4-first-run.json)；以上实测均在最终代码上完成，首次重跑除外
- Linux：[3.11](../../compat/results/v0.4.0/linux-3.11.json)、[3.12](../../compat/results/v0.4.0/linux-3.12.json)、[3.13](../../compat/results/v0.4.0/linux-3.13.json)，命令 `uv run python scripts/linux_verify.py`
- 构建产物与来源：[产物摘要](../../compat/results/v0.4.0/artifacts.json)、[上游源码清单](../../reference/provider-source-manifest.json)
- 历史：[0.3 报告](IMPLEMENTATION-0.3.0.md)、[0.3 验证存档](../../compat/results/v0.3.0/verification.json)、[0.2](../../compat/results/v0.2.0/verification.json)、[0.1](../../compat/results/v0.1.0/verification.json)

本机为 macOS ARM64。Linux 3.11/3.13 使用 ARM64 容器，3.12 使用 x86_64 容器。远程 CI 已配置，但本报告不把配置文件当作远端执行证据。
