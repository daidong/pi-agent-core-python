# 实施与验证结果（0.7.0 存档）

0.7 按三个目标改进：好装、多个 Python 版本可用、容易在上面搭完整的 agent。改之前，依赖锁死在精确版本上，只要环境里已有 `httpx<0.28` 之类的固定版本就装不进去；新用户最常见的六种工具写法，五种被拒绝，第六种（用普通函数当工具）运行时才失败。现在这些写法都能直接用，并补上了接本地模型、接 MCP 工具、从失败中恢复所需的部件。

## 要解决的问题

例子：研究者想让 agent 调用一个查文献的普通函数，并接到实验室服务器上的 vLLM。0.6 要手写 JSON Schema、把函数改成 `async`，而且没有 Chat Completions 接入，连不上 vLLM。0.7 中，给函数加上 `@tool`，再用 `OpenAICompletionsProvider(base_url=...)` 和 `llm.model("模型名")` 就能跑；在普通脚本里调用 `agent.prompt_sync(...)` 即可，不用自己写事件循环。

## 改了什么

| 目标 | 做了什么 |
|---|---|
| 好装 | 依赖改为版本范围：`jsonschema>=4.18,<5`；`[providers]` 为 `httpx>=0.27,<1`、`websockets>=14.2`、`PyJWT[crypto]>=2.8,<3`；新增可选的 `[mcp]` |
| 多版本 | 包元数据声明 3.11–3.14、无 GIL 构建和 PyPy；CI 配置加上 3.14、3.14t、PyPy、macOS、Windows，以及最低版本依赖测试 |
| 好用 | `@tool` 从带类型标注的函数生成工具；普通函数在线程里运行；工具返回值自动转换；接受 pydantic 和 MCP 生成的标准 schema，按 `format`、`pattern` 校验，与上游一致；`prompt_sync` 用于普通脚本 |
| 能搭完整 agent | Chat Completions 接入（本地模型和 OpenAI 兼容服务）；MCP 适配；出错判断函数与 `continue_run()` 重试；HTTP 错误带回服务器正文；新增 6 个可运行示例 |

功能完成后另做了一轮对抗性审查：专门用临时脚本找能复现的问题，共找到 13 个。12 个已修复并补了回归测试；1 个（准备钩子出错时多出一个 `turn_end`）与上游行为相同，保留不改。最重要的三个修复：
- 普通函数工具被取消后超过清理期限才结束时，Agent 会永久不可用；现在工具一结束就自动恢复。
- 从另一个线程调用 `abort()` 不能立即生效；现在可以。
- Python 自己的网络错误不被识别为可重试；现在可以识别。

## 验证结果

| 要回答的问题 | 证据 |
|---|---|
| 与上游的行为是否一致？ | 同一组输入分别交给固定上游代码和本库运行：核心循环 25 组、模型接入 50 组（新增 16 组 Chat Completions）、WebSocket 多轮 2 组、出错判断 44 条样例，全部一致 |
| 测试是否通过？ | Python 测试 308 项通过；上游相关测试 147 项通过；ruff、mypy 无问题 |
| 能否在 Linux 安装？ | Python 3.11、3.12、3.13、3.14 容器内断网安装 0.7.0 wheel，示例和测试各 304 项通过；跳过的 4 项需要 MCP SDK，断网安装用的锁文件不含它。3.12 镜像为 x86_64，在 arm64 主机上模拟运行，与以前的记录相同 |
| 其他 Python 实现和依赖版本？ | 本机安装 wheel 后，3.14t（无 GIL）308 项全部通过，PyPy 3.11 通过 304 项（4 项 MCP 测试跳过：MCP SDK 的服务器在 PyPy 上无法启动）。依赖取最低版本时 304 项通过；MCP SDK 1.10 配 jsonschema 4.20 时 308 项通过 |
| 能否与常见环境共存？ | 0.6 会冲突的 `httpx<0.28`、`jsonschema==4.23`、`websockets==14.2`、`PyJWT==2.8` 现在都能一起解析安装。旧版 gradio 要求 websockets 低于 13，仍与 `[providers]` 冲突；只装核心不受影响 |

## 仍未覆盖的部分

- 本机没有运行中的 Ollama、vLLM 等服务，Chat Completions 接入只用模拟服务器和上游差分验证过，尚未连接真实服务器。
- CI 配置里的 macOS 和 Windows 还没有实际运行；此目录没有远程仓库。
- 真实账号联调没有在 0.7 重跑。Claude、Codex 等现有接入的网络代码只改了两处：错误正文的处理，以及为兼容 websockets 14 而按版本决定是否传代理参数。两处都由本地测试和差分覆盖。
- 与上游相比，本库额外加了两处：最后一条回答失败后可以直接 `continue_run()` 重试；Chat Completions 的最终工具参数必须是完整合法的 JSON（上游会自动修补）。逐项清单见[验收映射](../../compat/COVERAGE.md#07-修订易用性与通用性)。

## 复现与验证附录

- 说明文档：[一页概念](../CONCEPTS.md)、[API](../API.md)、[模型接入与本地模型](../PROVIDERS.md)、[验收映射](../../compat/COVERAGE.md)
- 全部检查命令与输出：[verification.json](../../compat/results/v0.7.0/verification.json)，命令 `uv run python scripts/verify.py`
- 差分结果：[核心](../../compat/results/conformance.json)、[Provider](../../compat/results/provider-conformance.json)、[WebSocket 多轮](../../compat/results/websocket-conformance.json)、[出错判断](../../compat/results/recovery-conformance.upstream.json)（输入 `compat/recovery-cases.json`，命令 `scripts/recovery_conformance.py`）
- Linux 安装：[3.11](../../compat/results/v0.7.0/linux-3.11.json)、[3.12](../../compat/results/v0.7.0/linux-3.12.json)、[3.13](../../compat/results/v0.7.0/linux-3.13.json)、[3.14](../../compat/results/v0.7.0/linux-3.14.json)，命令 `uv run python scripts/linux_verify.py`
- 发布产物摘要：[artifacts.json](../../compat/results/v0.7.0/artifacts.json)
- 历史版本：[0.6.0](IMPLEMENTATION-0.6.0.md)（记录在 `compat/results/v0.6.0/`）、[0.5.0](IMPLEMENTATION-0.5.0.md)（`compat/results/v0.5.0/`）、[0.4.0](IMPLEMENTATION-0.4.0.md)、[0.3.0](IMPLEMENTATION-0.3.0.md)
