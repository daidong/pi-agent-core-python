# 实施与验证结果（0.9.0）

0.9 给 pi-python 加了插件机制：把工具、系统提示、钩子、技能、提示模板、子 agent 和 MCP 服务器打包成一个有名字、可以用 pip 安装的单元，加载后直接构造 agent。格式沿用 Pi 的 package；凡是 Pi 有对应代码的规则，都与 Pi 的实际输出做了对照，全部一致。执行循环没有改动，原有的各组差分结果不变。插件还没有用真实模型跑过，也还没有实际项目接入；在把插件格式当作稳定接口之前，应先补上这两项证据。

## 要解决的问题

以前，同一组工具、提示和规则要在每个项目里手写进 `Agent(...)`，不能整体分享，也不能整组打开或关掉。

例子：实验室有一套日志去重工具，配有一段"先读去重规则再动手"的说明、一条"不许删除原始数据"的检查，和一个负责复核的子 agent。0.8 里，每个用它的学生都要把这四样东西抄进自己的代码，原作者改了，抄过去的那份不会跟着变。0.9 里，它们放在一个目录里，或者打成 pip 包；`load_plugins(["./lab_tools"])` 加载以后，`plugins.agent(...)` 返回的 agent 已经带上全部四样。[examples/plugin_demo.py](../examples/plugin_demo.py) 离线运行的正是这个例子。

## 插件怎样工作

插件只做组装：收集各插件的内容，合成普通 Agent 的三样配置，即系统提示、工具列表和钩子。agent 本身照常运行，不知道插件的存在。有三点值得说明。

- **几个插件挂同一个钩子时怎样合并。** 规则照搬 Pi 的扩展运行器。工具调用前的检查，由第一个拦截的处理函数说了算；工具结果的修改和上下文变换依次传递。某个插件的处理函数出错时，只跳过它，其他照常运行。例如三个插件都处理工具结果，第一个改了文字，第二个抛出异常，第三个看到的是第一个改过的文字；最终结果包含第一个和第三个的修改，第二个的错误交给 `on_error` 记录。
- **技能按需读取。** 系统提示里只列出技能的名字和描述，模型需要时再调用 `read_skill` 读全文。技能再多，也不会一开始就占满上下文。
- **子 agent 是一个工具。** 主 agent 看到一个 `subagent` 工具，可以单独、并行或接力地把任务交给插件定义的子 agent。每个子 agent 有自己的历史；取消主 agent 时，子 agent 一并中止。

核心只改了一处：把"把部分更新应用到工具结果"的代码提取成函数，供插件模块复用，行为不变。

## 验证结果

| 要回答的问题 | 证据 |
|---|---|
| 技能、提示模板和文件头元数据（frontmatter）的规则是否与 Pi 一致？ | 69 条共享输入，加一个 13 项的技能目录和一个提示模板目录，用 Pi 自己的函数运行后逐项比较，全部一致。YAML 写错时两边的提示文字不同，这一项只比较出错的是哪个文件 |
| 内置的小解析器能否代替 YAML 库读取 frontmatter？ | 本机 2056 个真实的技能、子 agent 和命令文件中，两边都能解析的 2034 个结果相同。另有 13 个描述含 `: ` 的文件本库能读、YAML 报错；4 个页面模板本库不接受 |
| 原有行为是否改变？ | 核心循环 25 组、模型接入 50 组、WebSocket 多轮 2 组、出错判断 44 条，差分全部一致。Python 测试 398 项通过，比 0.8.2 多 88 项；上游相关测试 147 项通过；ruff、mypy 无问题 |
| 新用户照文档能否用起来？ | 在全新的 Python 3.11 环境里安装 0.9.0 wheel，和一个照文档打包的示例插件。按名字加载成功，技能和模板从 wheel 内读出，插件的工具正常运行。插件相关测试在 3.11、3.14t 和 PyPy 3.11 上通过 |
| 能否在 Linux 安装？ | Python 3.11、3.12、3.13、3.14 容器内断网安装 0.9.0 wheel，示例和测试各 390 项通过；跳过的 8 项需要 MCP SDK，断网安装用的锁文件不含它。3.12 镜像为 x86_64，在 arm64 主机上模拟运行 |

几项检查专门针对容易出错的地方：

- 把"`end` 优先于 `continue`"的合并规则故意改反，对应的测试会失败。
- 从两次不同的阻塞调用里先连接、后关闭同一个 MCP 连接，MCP SDK 会报"在另一个任务里退出取消范围"。因此每个 MCP 服务器在自己的任务里连接和关闭；在普通脚本里用 `with` 加载带 MCP 服务器的插件，测试通过。
- 读取技能文件时，指向技能目录以外的路径会被拒绝。

## 发布前审查

发布前做了一轮代码审查，并请两个独立的审查者分别从安全和架构角度检查。找到的问题都已修好，每项都有回归测试，并且确认去掉修复后测试会失败：

- **读取技能文件。** 在 Windows 上，`read_skill` 会先按模型给的路径访问文件系统，再检查是否在技能目录内；`//主机/共享` 这样的路径会先连接那台主机，可能泄露登录凭据的哈希。现在先按文字拒绝绝对路径、盘符和网络路径。
- **MCP 的密钥。** 环境变量里的 token 末尾多一个换行时，HTTP 库的报错会原样带出 token，进而出现在警告里；MCP SDK 1.10 还会把带 API key 的 header 跟着重定向发到别的源。现在拒绝含控制字符的 header 和 URL，警告里的环境变量值显示为 `***`，只在同一源内跟随重定向。CI 新增一个在 SDK 1.10 上运行 MCP 测试的任务。
- **frontmatter 解析。** 多行列表的解析耗时随长度平方增长，一个 40 KB 的文件要约 10 秒；嵌套过深会让所有插件都加载失败。现在逐行扫描，并限制长度和嵌套层数。
- **子 agent。** 并行任务里一个出错会中断整个调用；子 agent 的模型在构造时就定死，不跟随主 agent 的模型切换；子 agent 不受主 agent 的 `RunLimits` 约束。三处都已改正。
- **钩子合并。** 插件和应用挂同一个钩子时，应用自己的钩子返回错误类型的值，结果和没有插件时不同；async 的 `on_error` 不会被执行。现在两种情况都和预期一致，并加了逐个钩子的对照测试。
- **加载。** 入口点指向子模块时找不到插件目录；技能目录里的符号链接循环会让查找按指数增长。

审查后保留的两处设计都与 Pi 一致：子 agent 共用主 agent 的全部钩子，接力的步数不设上限。

## 仍未覆盖的部分

- 插件还没有用真实模型运行过，所有测试都用脚本化的模型。模型是否会按描述去读技能、是否会正确使用 `subagent` 的三种模式，还没有证据。
- 只有示例插件和测试里的插件。遥测处理等实际项目还没有接入；至少一个实际项目接入后，才能判断 `PluginAPI` 和资源格式是否够用、哪些需要改。在那之前，插件格式可能还会变。
- 多个处理函数的合并规则和 `subagent` 工具，与 Pi 的对应代码没有共享差分，只有 Python 测试。Pi 的这部分代码在 coding-agent 里，不在固定参照的核心测试范围内。
- `connect_http` 用到 MCP SDK 的一个内部函数 `create_mcp_http_client`。它在 SDK 1.10、1.30 和 2.3 上都存在，相关测试在这三个版本上都通过；SDK 以后改动这个函数时需要跟着调整。
- macOS 和 Windows 由 CI 运行；这批改动还没有推送，CI 还没有运行。
- Chat Completions 接入仍未连接真实的本地模型服务；真实账号联调没有重跑。

## 复现与验证附录

- 说明文档：[插件](zh/PLUGINS.md)、[一页概念](zh/CONCEPTS.md)、[API](zh/API.md)、[模型接入与本地模型](zh/PROVIDERS.md)、[验收映射](../compat/COVERAGE.md#09-新增插件)
- 全部检查命令与输出：[verification.json](../compat/results/verification.json)，命令 `uv run python scripts/verify.py`
- 差分结果：[核心](../compat/results/conformance.json)、[Provider](../compat/results/provider-conformance.json)、[WebSocket 多轮](../compat/results/websocket-conformance.json)、[出错判断](../compat/results/recovery-conformance.upstream.json)、[插件资源](../compat/results/plugins.upstream.json)（输入 `compat/plugin-cases.json` 和 `compat/plugin-fixtures/`，命令 `scripts/plugin_conformance.py`）
- Linux 安装：[3.11](../compat/results/linux-3.11.json)、[3.12](../compat/results/linux-3.12.json)、[3.13](../compat/results/linux-3.13.json)、[3.14](../compat/results/linux-3.14.json)，命令 `uv run python scripts/linux_verify.py`
- 历史版本：[0.8.0](history/IMPLEMENTATION-0.8.0.md)（记录在 `compat/results/v0.8.0/`）、[0.7.0](history/IMPLEMENTATION-0.7.0.md)（`compat/results/v0.7.0/`）、[0.6.0](history/IMPLEMENTATION-0.6.0.md)、[0.5.0](history/IMPLEMENTATION-0.5.0.md)、[0.4.0](history/IMPLEMENTATION-0.4.0.md)、[0.3.0](history/IMPLEMENTATION-0.3.0.md)
