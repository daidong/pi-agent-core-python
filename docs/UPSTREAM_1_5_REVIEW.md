# Pi v1.5.2 本地更新：兼容性初审

## 状态与范围

已核对 GitHub 最新正式 release 与远端 tag：目标为 v1.5.2，提交 `b4afdddf44e059c67ccf5097e5317d913521b470`（发布于 2026-11-10）。本地版本为 0.12.0，当前 HEAD 为 `13723fd`；既有核心差分基线为 v1.0.0，接口及部分 Provider 跟进到 v1.4.0。

首批三项已获批准并在本地实施：Anthropic 工具定义、逐消息模型的顶层 effort、Chat Completions 无当前工具时的请求。以下保留实施前审查作为迁移依据；不是全量 v1.5.2 移植，没有升级核心基线、包版本或运行依赖。开始时 AGENTS.md 与 CLAUDE.md 已有未提交修改，保留不动。

## 已核对的本地差异与建议

| 项目 | 已检查的证据 | 上层应用影响及建议 |
|---|---|---|
| Anthropic 非 strict 工具 schema 的共享定义 | 本地 `providers/anthropic.py` 构建 input_schema 时仅保留 type/properties/required；上游 `api/anthropic-messages.ts` 的 legacyInputSchema 另保留 `$defs` 和 `definitions` | 建议优先修复。涉及引用定义的 MCP/手写工具请求会改变；需检查代理请求校验和快照。验证初始工具与动态工具声明，不放宽执行端参数校验。 |
| Anthropic 逐消息 effort 模型的顶层 effort | 本地 managed 分支仍发送 high；上游 v1.5.0 发布说明将固定顶层值改为 low，解决组织 effort 上限拒绝 | 建议跟随。改变模型请求默认值；保留逐消息实际 effort，验证 low/medium/high 和显式覆盖。不是把所有模型的推理强度改成 low。 |
| Chat Completions 空 tools | 本地无当前工具但有工具历史时无条件发送 `tools: []`；上游默认省略，仅在 `requiresToolsWithToolHistory` 为 true 时发送 | 上游明确列为破坏性变化。建议跟随默认并提供旧行为开关。旧 LiteLLM/Anthropic 代理调用方需要设置该开关；vLLM 0.20+ 拒绝空数组。需请求载荷回归及旧代理配置验证。 |
| Responses 会话亲和头 | 本地 Codex 分支已有 `session-id`，普通 openai 分支只设置 x-client-request-id；上游依 compat.sessionAffinityFormat 设置 session-id | 不能全局把 session_id 文本替换成 session-id：Python option 名称仍为 session_id，Completions 是不同路径。需要按 Provider/端点逐项审查，再决定补齐范围。 |

这些修复仍会影响外部请求，不能以“公开函数签名不变”为由视为无兼容影响。按照项目决定，实施前需要用户确认上述范围。

## 建议独立处理的项目

- **增量 JSON 解析**：v1.5.0 已发布 StreamingJsonParser / ToolCallArgumentsStream；本地上轮明确暂缓。单独验证分片、转义、截断、非法最终 JSON、快照不可变性和所有 Provider/Proxy 路径。保留最终参数严格校验，不能顺带引入宽容执行。
- **Anthropic message threads 与 disableContinuation**：v1.5.1 引入服务器续接状态，涉及会话生命周期、并发、清理、回退以及代理转发。默认暂不开启，需单独说明与批准；不能当作纯缓存优化静默加入。
- **模型目录及 Usage/事件新字段**：独立审查目录增删、dataclass 序列化、消息编码和严格订阅器。不自动提升 schema 或更换目录。
- **pi-schema 类型转换**：上游 v1.5.0 改为统一转换参数；本地严格、不自动转换是已有明确差异。建议保留，不把 TypeScript 依赖迁移机械移植为 Python 行为变化。
- **新供应商、UI、durable 层**：本轮初始范围不纳入。

## 固定标签差异与本地离线核查（实施前）

对官方两个标签运行 `git diff --stat v1.4.0 v1.5.2 -- packages/agent/src`，仅 `proxy.ts` 和 `types.ts` 有差异；`agent.ts` 与 `agent-loop.ts` 无源码差异。这不代表间接依赖不变。前者改为按调用维护流式参数解析器并转发 `disableContinuation`，后者改变 TS 工具参数/输出 schema 类型合同。Python 代理已把原始分片放在独立字典，结束时还会核验分片与最终参数是否一致，不能照搬 TS 差异。首批三个请求修复属于 **pi-ai 的 Provider**，不是 agent-loop 改写。

用本地 Provider 构造实际请求体（不访问模型）观察到：

- `claude-opus-5-5` 的工具属性 `$ref: '#/$defs/P'` 在请求中仍在，但传入的根定义 `$defs.P` 消失，产生悬空引用。初始工具和内联工具共用 `declarations`；本次动态探针只测初始路径。
- 同一请求设 `reasoning=medium` 时顶层 effort 为 `high`，消息上的 effort 为 `medium`。应只改前者；既有 `test_claude_agent_tool_and_effort_changes_use_native_blocks` 明确断言旧的顶层值，升级时不能把两层断言一起替换。
- 用含 assistant 工具调用的历史、无当前工具构造 Chat Completions 请求，分别设 `requiresToolsWithToolHistory=False/True`，两次都观察到 `tools: []`。现有 `resolve_compat` 仅合并已识别的键，提前在旧版设置开关并不能改变行为。迁移旧网关时可先配 true，再升级库。

基线命令 `.venv/bin/python -m pytest -q tests/test_session_changes.py tests/test_completions.py`：50 通过。它只验证当前版本，并非 v1.5.2 对齐证明；没有真实网关、账户或 v1.5.2 双端运行验证。

## 实施获批后的验证计划

1. 用户确认受影响的第一批范围后，为每项补充可复现回归，再改实现。
2. 固定新增参照来源与摘要，不覆盖旧基线，不手写上游 expected 冒充对照。
3. 跑定向 Provider 测试、真实固定上游差分、类型/格式检查及完整库内回归。
4. 记录外部代理、真实账户和上层应用未验证的范围；离线测试通过不代表部署兼容。

## 首批实现与字段对照

应用迁移见[模型接入指南](zh/PROVIDERS.md)。旧版 v1.4.0 差分仍运行并保留原始输出；仅两个具名 fixture 的顶层 effort 改与新版真实观测值比较。根定义与空工具列表的 Python 回归也使用[字段观测](../compat/provider-1.5-fields.json)，不是整个 Provider 都使用新版参照。上游请求通过 `onPayload` 捕获，在网络发送前停止。

复现（Node 开发环境，不进入包依赖）：

```sh
tmp=$(mktemp -d)
npm install --prefix "$tmp" --ignore-scripts @earendil-works/pi-ai@1.5.2
node reference/provider-1.5-probe.mjs "$tmp/node_modules/@earendil-works/pi-ai" > "$tmp/fields.json"
diff -u compat/provider-1.5-fields.json "$tmp/fields.json"
```

此次使用的 pi-ai npm 包 integrity 为 `sha512-2tWPSnV3qzfiDIY3aWluBag3vC++Vs+VsXt/UquSU5onPMohPih72FUhIhaqmDdMYubOgB3Byl9uF4Vk40JLvQ==`。不对真实网关或账户可用性作保证。

## 来源

- [最新正式 release](https://github.com/earendil-works/pi/releases/tag/v1.5.2)，另通过 `git ls-remote --tags` 核对 tag 提交。
- [固定 Agent changelog](https://github.com/earendil-works/pi/blob/b4afdddf44e059c67ccf5097e5317d913521b470/packages/agent/CHANGELOG.md)
- [固定 AI changelog](https://github.com/earendil-works/pi/blob/b4afdddf44e059c67ccf5097e5317d913521b470/packages/ai/CHANGELOG.md)
- [Anthropic 实现](https://github.com/earendil-works/pi/blob/b4afdddf44e059c67ccf5097e5317d913521b470/packages/ai/src/api/anthropic-messages.ts)
- [Completions 实现](https://github.com/earendil-works/pi/blob/b4afdddf44e059c67ccf5097e5317d913521b470/packages/ai/src/api/openai-completions.ts)
- [Responses 实现](https://github.com/earendil-works/pi/blob/b4afdddf44e059c67ccf5097e5317d913521b470/packages/ai/src/api/openai-responses.ts)

这是一份首批兼容性审查，不是所有 v1.4.0→v1.5.2 提交的完整审计。
