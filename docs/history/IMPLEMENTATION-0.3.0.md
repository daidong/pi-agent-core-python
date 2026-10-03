# 实施与验证结果（0.3.0 存档）

这次补齐了核心消息与事件合同，扩大了与固定 Pi 上游的请求协议对照，并用本地真实授权完成 Claude、Codex 和 DeepSeek 联调。模型能力表、受限重试和 WebSocket 复用已加入；可以开始把这些 Provider 用到应用集成中，但不能据此宣布整个 pi-ai 或 pi-mono 已完全移植。

例如，模型请求执行 `add(2, 3)` 时，核心先接收工具块开始、参数增量、块结束和整条消息完成。只有完整内容一致且参数通过验证后，工具才执行；结果返回模型产生 `5`。真实 Codex WebSocket 测试中，这个两轮循环共建立一个连接，第二轮复用连接，工具只执行一次。

| 本次补齐的能力 | 对应上游范围 | 当前结果 |
|---|---|---|
| 消息、工具结果和钩子上下文 | pi-ai 数据类型与 pi-agent-core | 系统文本块、响应 ID/实际模型、请求与原生推理等级、诊断、工具 details/usage/nested_calls、新增消息集合 |
| 模型事件 | pi-ai 的流事件合同 | start、块开始/增量/结束、partial、done/error；保留旧注入 Provider 的 final 接口 |
| 请求及签名重放 | Claude Messages、OpenAI/Codex Responses | 补缓存、工具格式、会话请求头、签名 API 判断及最终加密签名补全 |
| 模型能力 | pi-ai 模型目录与推理等级映射 | 随包 71 条可替换的快照记录；自动选择推理模式，检查已知模态和输出上限 |
| 网络恢复 | pi-ai HTTP 与 Codex transport | 明确 HTTP 拒绝后的有限重试、Retry-After、取消、握手失败回退、会话和账号隔离的连接复用 |
| DeepSeek API | 新增适配 | 使用官方当前 Responses 协议；不冒充固定 Pi 中的 Chat Completions 路径 |

执行循环参照 pi-agent-core，模型协议和鉴权参照 pi-ai；pi-mono 还包含应用层。这里比较的是固定 Pi v1.0.0 的已覆盖行为，不是当前所有上游功能。

## 验证结果

| 要验证的问题 | 证据 |
|---|---|
| 新合同与原有执行逻辑能否一起工作？ | 212 项 Python 测试通过 |
| 核心是否回归？ | 21 组共享输入与实际固定上游相同 |
| 请求和流是否与上游一致？ | 22 组 Provider 输入对照内容、签名、停止原因、事件顺序/载荷；非代理路径还对照请求体和语义请求头 |
| 固定参照本身是否可运行？ | 77 项 agent 测试和 20 项 transcript/validation 测试通过 |
| 本地真实授权是否可用？ | Claude/Codex 订阅及 DeepSeek API 均完成文本、工具、推理和签名历史重放 |
| 连接是否真的复用？ | Codex 实际两轮请求：建连 1 次、复用 1 次、工具执行 1 次 |

真实联调最终包含三个接入、每个三类场景，共九项传输与结果检查。推理场景另发一次后续请求验证签名历史重放。文本和工具场景检查完整答案；算术场景检查答案最后的整数等于 19、收到推理块、重放成功。**Claude 算术答案正确，但没有遵守“只输出数字”的格式要求**，因此不能把九项通过解释成九项严格格式通过。最初还出现一次输出预算耗尽，以及一次 WebSocket 重复响应头错误；失败与修复记录均保留在验证附录中。

发行版本为 0.3.0。Ruff、mypy、wheel 和源码包构建通过。Linux Python 3.11、3.12、3.13 使用构建后的 wheel 验证核心单独安装、可选依赖安装和全部测试；详细状态以附录的实际输出为准。

## 设计选择与剩余功能

严格参数验证、状态副本、未知工具结果不重放继续保留。这些是原设计选择，不列为缺失功能。部分事件中的快照与上游可变对象不同，完整流验证也更严格；但允许上游定义的最终加密推理签名补全，不能误判为内容损坏。

仍未补齐供应商内建搜索/grammar tools、原生会话内系统/工具/effort 变更协议、后台任务轮询、音视频生成、模型目录自动更新，以及完整应用层的 MCP、TUI、持久化恢复。缓存 WebSocket 目前发送完整上下文，没有移植 previous_response_id 增量请求与其专有恢复分支。上下文窗口长度提供为元数据，本地没有 tokenizer 或自动压缩；价格只按快照基础费率估算。

OpenAI API key、ChatGPT 直接 OAuth、新登录和刷新尚未做本次账号侧验证。真实服务端限流和断网没有人为制造，重试及故障边界由本地可控测试验证。模型表是本地 Pi 生成目录快照，不能保证实时模型可用性或账号权限。下一轮应优先覆盖应用实际需要的原生会话变更和登录刷新，再按业务需要扩展供应商专有功能。

## 复现与验证附录

- [API 合同与迁移](../API.md)、[Provider、模型表与网络配置](../PROVIDERS.md)、[实际接入示例](../../examples/provider_chat.py)
- [检查命令与完整输出](../../compat/results/v0.3.0/verification.json)：`uv run python scripts/verify.py`
- [核心差分](../../compat/results/conformance.json)、[Provider 差分](../../compat/results/v0.3.0/provider-conformance.json)
- [最终账号联调](../../compat/results/live-final-matrix.json)、[最终 WebSocket 复用](../../compat/results/live-websocket-final.json)：由 `scripts/live_providers.py` 显式读取指定账号来源；不进入 CI，不保存凭据或模型正文
- [初轮推理预算不足](../../compat/results/live-providers.json)、[格式检查与历史重放](../../compat/results/live-reasoning-replay.json)、[Claude 算术及重放复核](../../compat/results/live-claude-reasoning-replay.json)
- [重复响应头故障](../../compat/results/live-websocket-cached.json)、[修复后复测](../../compat/results/live-websocket-cached-recheck.json)；回归测试见 `tests/test_recovery.py`
- [Linux 3.11](../../compat/results/v0.3.0/linux-3.11.json)、[3.12](../../compat/results/v0.3.0/linux-3.12.json)、[3.13](../../compat/results/v0.3.0/linux-3.13.json)：`uv run python scripts/linux_verify.py`
- [构建产物摘要](../../compat/results/v0.3.0/artifacts.json)、[模型表及来源](../../src/pi_python/data/models.json)、[上游源码摘要](../../reference/provider-source-manifest.json)
- [0.2 验证存档](../../compat/results/v0.2.0/verification.json)、[0.1 验证存档](../../compat/results/v0.1.0/verification.json)

本机为 macOS ARM64。Linux 3.11/3.13 使用 ARM64 容器，3.12 使用 x86_64 容器。远程 CI 已配置，但本报告不把配置文件当作远端执行证据。
