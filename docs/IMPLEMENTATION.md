# 实施与验证结果（0.8.0）

0.8 去掉了为本库旧版本保留的兼容代码和升级说明。项目还在内部开发阶段，不需要照顾旧版本的使用者。执行行为、公开接口和与上游 Pi 的一致性都没有变化：全部差分和测试的结果与 0.7 相同。

## 改了什么

- **消息编码**：`decode_messages` 只接受当前的格式版本 3。以前它还接受版本 1 和 2，现在这两个版本和其他未知版本一样被拒绝。
- **文档**：`docs/API.md` 删掉了三节版本迁移说明；两个标题去掉了版本号，描述的仍是当前功能；文中讲旧版本行为的句子也一并删掉。设计文档和有意偏离表里的旧版本注记同样删掉了。
- **过时描述**：有意偏离表 D4 一行原来写着"逾期仍在运行的工具使实例不可再用"，实际上 0.7 起工具结束后实例会自动恢复，已改正。

下面这些也带有"兼容"字样，但它们不是为本库旧版本服务的，所以保留：
- **与上游 Pi 的对照**：`compat/` 目录、差分测试、`UPSTREAM-COMPATIBILITY.md`。
- **对第三方较旧依赖的支持**：websockets 14、MCP SDK 1.x。
- **历史记录**：`docs/history/` 和 `compat/results/v0.x/` 里的旧版报告与验证记录。

## 验证结果

| 要回答的问题 | 证据 |
|---|---|
| 与上游的行为是否一致？ | 核心循环 25 组、模型接入 50 组、WebSocket 多轮 2 组、出错判断 44 条样例，全部与固定上游的实际运行结果一致 |
| 测试是否通过？ | Python 测试 308 项通过；上游相关测试 147 项通过；ruff、mypy 无问题 |
| 能否在 Linux 安装？ | Python 3.11、3.12、3.13、3.14 容器内断网安装 0.8.0 wheel，示例和测试各 304 项通过；跳过的 4 项需要 MCP SDK，断网安装用的锁文件不含它。3.12 镜像为 x86_64，在 arm64 主机上模拟运行，与以前的记录相同 |

## 仍未覆盖的部分

- Chat Completions 接入尚未连接真实的本地模型服务；
- 真实账号联调没有重跑。

0.8.0 发布时 macOS 和 Windows 还没有实际运行过。之后 CI 在 GitHub 上首次运行，发现并修复了 Windows 和 PyPy 上的问题，见[验收映射](../compat/COVERAGE.md#080-之后的跨平台修复)。

## 复现与验证附录

- 说明文档：[一页概念](CONCEPTS.md)、[API](API.md)、[模型接入与本地模型](PROVIDERS.md)、[验收映射](../compat/COVERAGE.md)
- 全部检查命令与输出：[verification.json](../compat/results/verification.json)，命令 `uv run python scripts/verify.py`
- 差分结果：[核心](../compat/results/conformance.json)、[Provider](../compat/results/provider-conformance.json)、[WebSocket 多轮](../compat/results/websocket-conformance.json)、[出错判断](../compat/results/recovery-conformance.upstream.json)
- Linux 安装：[3.11](../compat/results/linux-3.11.json)、[3.12](../compat/results/linux-3.12.json)、[3.13](../compat/results/linux-3.13.json)、[3.14](../compat/results/linux-3.14.json)，命令 `uv run python scripts/linux_verify.py`
- 发布产物摘要：[artifacts.json](../compat/results/artifacts.json)
- 历史版本：[0.7.0](history/IMPLEMENTATION-0.7.0.md)（记录在 `compat/results/v0.7.0/`）、[0.6.0](history/IMPLEMENTATION-0.6.0.md)、[0.5.0](history/IMPLEMENTATION-0.5.0.md)、[0.4.0](history/IMPLEMENTATION-0.4.0.md)、[0.3.0](history/IMPLEMENTATION-0.3.0.md)
