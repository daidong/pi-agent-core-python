# MCP 宿主修复与部署验收

发布候选为 `0.10.0`。下文记录修复阶段的本地验收，其内部构建当时仍标记为 `0.9.0`。
正式上传前须通过发布分支的完整 CI；发布工作流已将它设为必需的前置条件。

本次修复并验证了审查发现的三项问题：表单正则阻塞宿主、未知用量被计为零、断连服务仍通过就绪检查。
三项回归和安装包验收均已通过，可以进入目标预发布环境的业务验收。
本次覆盖 macOS 和 Linux 容器；Windows 与真实模型服务尚未实测，不能据此判定所有部署环境都已就绪。

例如，模型接口返回 `usage: {}` 时，现在计量回调收到 `None`，表示未知；
接口明确返回输入和输出各零 token 时，仍记录为真实零用量。
不完整的计数继续保留给直接调用者查看，但不会当作完整测量用于计费。

表单现在会在编译 schema 和调用界面之前拒绝 `pattern` 约束，包括普通正则。
这收窄了原先的支持范围；长度、枚举和支持的格式约束继续可用。
就绪检查会并发 ping MCP 会话，每个会话默认最多等待 5 秒，可用 `mcp_timeout` 调整。
失败或超时的服务及其能力不计入快照，后续检查成功可以恢复；业务错误不会自动被视为断连。

公共接口和运行方法见 [交互文档](MCP_INTERACTION.md#真实-provider配置选择与计量)。
[宿主示例](../../examples/mcp_interactive.py) 使用允许的命名配置，在同一任务中交替完成工具调用、
JSON 辅助调用、普通文本调用和人工表单。它不需要真实 API 密钥，表单答案为模拟用户选择。

## 实际结果

| 检查 | 结果 |
|---|---|
| macOS / Python 3.11，最终 wheel 全量回归 | 612 通过，2 跳过 |
| Linux ARM64 / Python 3.11，安装包全量回归 | 611 通过，2 跳过；最终 wheel 定向回归 57 通过 |
| Python 3.11、MCP SDK 2.3.0 的交互、迁移与进程验收 | 163 通过 |
| Python 3.12、MCP SDK 1.10 的旧工具及插件路径 | 129 通过，2 跳过 |
| 最低依赖版本，未安装可选 MCP 功能 | 435 通过，34 跳过 |
| 核心上游兼容比较 | 25 项通过 |
| 上游 Provider 夹具比较 | 50 个夹具全部通过 |
| 插件兼容比较 | 7 项通过 |
| Ruff、格式检查、mypy、差异空白检查 | 全部通过 |
| wheel 独立安装、公开能力标识与离线示例 | 通过 |

新旧 SDK 的跳过项分别要求不兼容的元数据接口，在对应环境中互补验证。
最低依赖环境未安装 MCP，其相关测试按预期跳过。安装包验收从 `site-packages` 导入，
没有依赖可编辑安装。Linux 使用只读挂载的测试文件；安装包及测试依赖位于容器内。
CI 已增加从 wheel 安装后、在工作树外执行离线交互示例及三项修复回归的步骤。

## 验证方法

检查日期：2026-10-04。只修改 pi-python，没有调用付费模型 API，没有执行 Telemetry 项目的测试。
验收使用 localhost HTTP/SSE 服务驱动实际 OpenAI Responses、Anthropic 和兼容 Chat Completions
解析器。MCP 服务运行在独立进程中，分别通过 stdio 和 streamable HTTP 连接。

新增回归在修复前确认失败。覆盖空和不完整用量、真实零用量、choice 内用量、末尾空片段，
以及表单约束拒绝、空闲和调用中的服务退出、探活超时、取消、恢复与普通业务错误。
Linux 全量通过后，最后补充了“后续片段缺少用量时仍保留已有部分计数”的兼容修复。
最终 wheel 在 Linux 重跑了全部三项定向回归，并在 macOS 重跑完整测试集。

[新增迁移测试](../../tests/test_mcp_host.py) 覆盖：

- 真实解析器产生的文本、工具轮次，以及推理与工具调用同时出现的回复；检查下一轮发送给供应商的标识和签名。
- 同一任务的命名配置选择、真实 JSON 辅助回复、请求级凭据和三个回调，以及 Anthropic 禁止并行工具调用的参数。
- 用量归属、缓存字段、未知用量、重试后的独立响应、观察函数失败时不重复生成。
- 限流、暂时性服务错误、永久拒绝、可中断的等待、脱敏错误数据，以及已完成工具不被重放。
- 两条并发连接的状态隔离；“宿主 → 业务服务 → 沙箱服务”的模型往返与取消。
- 取消后释放保留的供应商状态；宿主绑定方法保持原对象身份；不支持的传输模式在模型访问前失败。

[原有 MCP 测试](../../tests/test_mcp.py) 和 [交互测试](../../tests/test_mcp_interaction.py) 继续覆盖表单接受、拒绝、取消、超时与校验，
服务断连、连接关闭、工具超时、会话复用、插件授权，以及 SDK 的进度和取消行为。

macOS 与 Linux 的 POSIX 进程清理均已测试，包括协作式进程作用域内的嵌套连接。
绕过该作用域独立启动或脱离的进程仍须由应用管理。MCP 通信与进程分离不是安全沙箱。

## 复跑

```sh
.venv/bin/python -B -m pytest -q -ra -p no:cacheprovider
.venv/bin/ruff check src tests examples compat scripts
.venv/bin/ruff format --check src tests examples compat scripts
.venv/bin/mypy src/pi_python

uv run --isolated --python 3.11 --extra mcp-interactive --with 'mcp==2.3.0' \
  python -B -m pytest -q -ra -p no:cacheprovider \
  tests/test_mcp_interaction.py tests/test_mcp_host.py tests/test_mcp_process.py

uv run --isolated --python 3.12 --extra providers --group dev --with 'mcp>=1.10,<1.11' \
  python -B -m pytest -q -ra -p no:cacheprovider tests/test_mcp.py tests/test_mcp_process.py tests/test_plugins.py

.venv/bin/python scripts/provider_conformance.py
.venv/bin/python scripts/plugin_conformance.py
uv build --out-dir dist/acceptance
```

上游比较脚本会刷新 `compat/results` 中的记录。本次运行后保留了这些文件运行前的内容，
验收结论记录在本文。Provider 比较使用本地固定夹具，不代表线上模型服务验证。

## 兼容边界

状态保留到外层工具调用结束，不支持跨任务恢复或宿主重启后恢复。服务端看不到供应商思考块，
也不能以标准 Sampling 回复中的空用量字典计费。音频、助手图片、命名空间或内置服务器工具、
延迟结果和模型 WebSocket 模式不在本次支持范围。普通旧 MCP 工具路径继续兼容 SDK 1.10。

版本号仍为 `0.9.0`。应检查公共 `MCP_FEATURES` 能力集合，不能单凭版本号识别本次构建。
此构建在本地生成并安装验证，没有发布到 PyPI。安装步骤和能力检查已加入文档及 CI。

## 本地构建

文件：[dist/acceptance/pi_python_core-0.9.0-py3-none-any.whl](../../dist/acceptance/pi_python_core-0.9.0-py3-none-any.whl)。

SHA-256：`92deada35910cc265ed68e330f600f6154b44602035098a057148df33dcd85c8`。

```python
from pi_python.mcp import MCP_FEATURES
assert {
    "sampling-host-state-v1", "sampling-profiles-v1",
    "sampling-metering-v1", "sampling-retry-v1",
} <= MCP_FEATURES
```

后续重新构建时，应重新计算文件摘要；公共能力标识用于检查接口契约。

## 验证附录

本地验收日志随构建保存在 `dist/acceptance`，不进入版本控制：

- [最终 wheel 的 macOS 全量结果](../../dist/acceptance/macos-wheel-full.log)
- [Linux 全量结果](../../dist/acceptance/linux-checks-final.log)与[最终 wheel 定向回归](../../dist/acceptance/linux-final-wheel-regressions.log)
- [最低依赖最终结果](../../dist/acceptance/lowest-dependencies-final.log)
- [独立安装及离线示例](../../dist/acceptance/installation-checks.json)
- [最终 Provider 兼容比较](../../dist/acceptance/provider-conformance-final.log)

最初的额外验收因测试夹具使用相对路径而在错误工作目录收集失败。
改用项目目录运行全量夹具后通过；三项定向安装包验收仍在工作树外执行。
Linux 精简镜像补装了进程测试需要的 `procps`。这些属于验收环境修正。
版本仍为 `0.9.0`，没有发布到 PyPI，也没有操作远端生产环境。
