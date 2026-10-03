# 实施与验证结果（0.5.0 存档）

这一轮按方案 B 向 Pi 的写法靠拢：去掉与上游并存的旧事件写法，让模型能力只有一个来源，拆分过大的 Agent 类，并强制类型标注。执行行为没有变：与固定 Pi 的全部差分输入和真实账号测试都与 0.4 结果一致。公开接口有四处变化，已有代码中如果自己写了 Provider，或使用了模型表之外的模型名，需要按[迁移表](../API.md#05-迁移)修改。

## 为什么改

上一轮的评估结论是：入门和 Pi 一样简单，但同一件事常有两种写法，读代码时要同时记住两套规则。0.5 只处理"多出来的写法"，不动当初为科研场景选的可靠性机制（严格参数检查、运行上限、结果未知时停止）。

最能说明问题的是表外模型。0.4 中，`Agent(provider=AnthropicProvider(...), model="claude-new-x")` 不会报错：Provider 查不到这个名字，就悄悄按"预算式推理、最多输出 4096 个 token"的默认值发请求。新模型如果其实使用自适应推理、输出上限是 128k，请求会被截短或推理参数不对，而表面上一切正常。0.5 中同样的写法会报 `ConfigurationError`，并说明两种解决办法：传一个 `ModelInfo`，或把它登记进模型表。Pi 也是这样做的：它把完整的模型对象交给 Provider，没有隐藏的默认值。

## 改了什么

| 内容 | 0.4 | 0.5 |
|---|---|---|
| Provider 事件 | 上游写法与旧写法（`final`、按调用 ID 的参数增量、可不开块的增量）并存，由两段检查代码分别处理 | 只保留上游写法；所有 Provider 的事件经同一个检查器；Agent 处理模型流的代码从 185 行减到 92 行 |
| 模型能力 | 模型表、Claude Provider 的三个覆盖参数、表外默认值三处来源 | 只来自 `ModelInfo`；Agent 可直接接受 `ModelInfo`；Provider 里的"模型未知"分支全部删除 |
| Agent 结构 | 一个 670 行的类同时管会话、一次运行、队列和清理 | 会话（Agent，348 行）、一次运行（Run，341 行）、输入队列（76 行），对应上游 `agent.ts` 与 `agent-loop.ts` 的分工 |
| 类型标注 | 约 90 个函数没有标注，主要在 Provider 层 | 全部补齐，mypy 现在拒绝未标注的函数；两处 `__import__` 和多数函数内导入移到文件顶部，只保留三个可选依赖的延迟导入 |
| 入门文档 | 无 | [一页概念](../CONCEPTS.md)：五个概念、一轮的流程图和可运行的最小例子 |

补类型时发现一处真实缺口：OpenAI 和代理流没有检查事件中的块位置是否为整数。格式错误的事件以前会产生令人误解的报错，现在明确报"缺少位置"。另外，`message_update` 事件的两种数据格式合并成一种，迁移表里列出了。

总代码量没有变少：核心约 2,900 行，0.4 为约 2,850 行，增加的主要是类型标注和说明。这一轮减少的是"同一件事的写法数量"，不是行数。公开名称仍为 73 个。

## 验证结果

| 要回答的问题 | 证据 |
|---|---|
| 执行行为是否改变？ | 核心 21 组、Provider 34 组、WebSocket 多轮 2 组输入，与固定上游的结果全部相同 |
| 旧的 Provider 输入是否仍可比？ | 20 组旧输入改走上游 `streamSimple` 入口（Agent 实际使用的路径），并使用与上游运行器相同的测试模型记录；两组重放输入补上了上游必需的用量字段 |
| 测试是否通过？ | Python 测试 228 项通过；上游相关测试 147 项通过 |
| 真实服务是否不受影响？ | Claude、Codex 会话中途变更实测 2 项通过（Codex 后三个请求仍是增量）；三个接入的文本、工具、推理及签名重放 9 项通过 |
| 能否在 Linux 安装？ | Python 3.11、3.12、3.13 容器内断网安装 wheel，示例和全部测试通过 |

## 仍未覆盖的部分

可靠性机制仍比 Pi 多。它们是有意保留的设计选择，[一页概念](../CONCEPTS.md)最后一节单独列出，不需要时可以不读。

登录和令牌刷新已在 0.5 发布后补测，Claude 与 Codex 均通过，见下一节。OpenAI API key 和 ChatGPT 直接授权仍没有实测；约束采样、后台任务轮询、应用层功能仍未实现。

## 登录与令牌刷新实测

用你通过例子 CLI 新登录得到的 Claude 和 Codex 凭据文件，各做了三次真实刷新。测试把令牌标记为已过期，模拟它在长会话中自然到期，不需要等几个小时。

| 要回答的问题 | Claude | Codex |
|---|---|---|
| 三个调用者同时发现令牌过期，会刷新几次？ | 1 次，三者拿到同一新令牌 | 同左 |
| 新令牌是否原子写回文件、权限仍为 0600？ | 是 | 是 |
| 会话中途令牌过期，请求是否自动刷新后成功？ | 是 | 是 |
| 只读文件再刷新一次是否成功（保存的是有效的刷新令牌）？ | 是 | 是 |
| 例子 CLI 不加 `--login` 能否直接复用文件？ | 是 | 是 |

两边的刷新令牌每次都会轮换，旧的随之作废。所以实测只能用本库自己登录得到的文件；用 Claude Code 或 Codex CLI 的文件会让它们掉线。多个进程同时使用同一凭据文件仍不支持：例子只保证单进程，跨进程需要应用自己加文件锁。记录见 [live-refresh.json](../../compat/results/live-refresh.json)，命令为 `uv run --extra providers python scripts/live_refresh.py --anthropic-file ... --codex-file ...`。

## 复现与验证附录

- 说明文档：[一页概念](../CONCEPTS.md)、[API 与 0.5 迁移](../API.md)、[模型接入](../PROVIDERS.md)、[验收映射](../../compat/COVERAGE.md)
- 全部检查命令与输出：[verification.json](../../compat/results/v0.5.0/verification.json)，命令 `uv run python scripts/verify.py`
- 差分结果：[核心](../../compat/results/conformance.json)、[Provider](../../compat/results/provider-conformance.json)、[WebSocket 多轮](../../compat/results/websocket-conformance.json)
- 实测（`scripts/live_providers.py`，不保存凭据或模型正文）：[Claude 会话变更](../../compat/results/live-0.5-session-claude.json)、[Codex 会话变更与增量](../../compat/results/live-0.5-session-codex.json)、[三接入回归](../../compat/results/live-0.5-regression.json)
- Linux：[3.11](../../compat/results/v0.5.0/linux-3.11.json)、[3.12](../../compat/results/v0.5.0/linux-3.12.json)、[3.13](../../compat/results/v0.5.0/linux-3.13.json)；[构建产物](../../compat/results/v0.5.0/artifacts.json)
- 历史：[0.4 报告](IMPLEMENTATION-0.4.0.md) 与[验证存档](../../compat/results/v0.4.0/verification.json)、[0.3 报告](IMPLEMENTATION-0.3.0.md)

本机为 macOS ARM64。Linux 3.11/3.13 使用 ARM64 容器，3.12 使用 x86_64 容器。
