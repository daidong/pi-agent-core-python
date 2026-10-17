# 应用直接执行工具：实现与验证

这次检查的是：应用能否不经过模型决策，直接执行已注册工具，同时保留与模型调用相同的执行规则。现在两种入口共用工具执行路径，应用调用不会伪装成模型调用；无网络示例也验证了“运行完成，但领域检查未通过”。这些结果支持开始在应用侧组合具体能力包，不代表领域方法或科研判断已经实现。

## 新设计如何分工

- **机械 core** 管模型请求、工具执行、参数验证、钩子、事件、预算和取消，不判断领域结论是否正确。
- **领域能力包** 提供知识、工具、固定步骤和验证规则。应用用普通 Python 代码组合它们，拥有状态与生命周期，并解决名称和规则冲突。
- **基础模型** 使用这些知识处理具体任务。哪些步骤由模型选择、哪些由应用固定执行，由应用明确决定。

新增的 `Agent.execute_tools([ToolInvocation(...)], source=...)` 让应用可以执行固定检查，而不必先让模型选择工具。它与模型调用共用参数准备、执行前后钩子、输出验证、并发限制、工具预算和取消清理。每批是独立运行；跨批次总预算仍由应用管理。

例如，[组合示例](../examples/capability_composition.py) 检查数值 `-1` 是否达到应用规定的下限 `0`。模型可以选择调用检查工具；应用也可以先执行同一个工具，再把结果交给模型解释。检查程序正常运行，所以运行状态是 `completed`；数值没有达标，所以应用读取的领域验证结果仍是 `False`。

## 记录如何保持可解释

工具上下文和执行结果通过只读属性提供 `origin` 与 `source`，分别表示调用来源和应用自报的诊断标签，不改变原有 dataclass 序列化字段。工具事件默认不新增这两个键，只有显式设置 `Agent(include_tool_origin=True)` 才输出。来源标签不代表认证身份。模型调用标为 `model`，应用调用标为 `application`；应用批次历史始终保存来源。

应用批次不请求模型，不产生模型轮次，也不消费指导或后续输入队列。历史保存一条内置的 `application_tool_batch` 自定义消息，包括各调用的参数、来源、执行状态和整理后的可见内容。后续请求将它转成明确标注的应用结果文本，不制造 assistant 工具调用或悬空工具结果。原始结果和本地详情留在执行结果中，不自动发给模型。

工具名冲突由 core 拒绝。指令段冲突、多个钩子的调用顺序和否决规则由示例中的应用组合器处理。两个能力包实例的可变状态彼此独立。本次没有新增领域包加载器，也没有改动现有插件机制。

## 旧应用的兼容性

按用户要求，来源信息不再默认扩展工具事件或 dataclass 序列化结构。专门的测试使用 v0.10.1 的字段集合，验证严格检查字段的旧消费者仍能走完模型工具循环。来源仍可通过属性显式读取，需要事件记录时再开启选项。

只使用原有 `prompt/continue_run` 且不导入新批次历史的应用，不会收到空轮次或新批次消息。采用 `execute_tools` 后则需要检查接入边界：监听器对 `turn_id=None` 做整数运算会导致订阅失败；已有消息转换器若拒绝新类型，会使后续模型运行失败，若过滤所有自定义消息，则会静默丢掉检查结果。没有自定义转换器时，core 可直接处理内置批次消息；有转换器时，原样保留这类消息也可交给 core 处理。相关成功与失败情况均有测试，但未运行所有实际上层应用的集成测试。

## 验证结果与边界

兼容性修订后的本机全量测试 **798 项通过、2 项跳过**。C28、C29 及兼容性定向测试共 **47 项通过**，覆盖零模型请求、来源与消息编解码、共享执行规则、配置隔离、忙碌互斥、取消与未退出工具、订阅故障和领域验证失败。两项跳过来自既有插件测试：它们只适用于不支持 `meta` 的旧版 SDK，不计入通过数。

实际运行上游和 Python 后，**25 组核心差分全部通过**。新增入口属于 Python 合同 D9，不计作上游直接一致。构建后的 wheel 已在仓库外的独立环境离线安装；43 项应用批次与兼容性测试及组合示例通过。

本报告记录发布准备前的本地验证，环境为 macOS ARM64、Python 3.14。该轮没有重新运行 Linux/HPC、多版本矩阵或真实模型调用；已把新增示例和安装包测试接入现有 CI。0.10.2 的接入说明见[发布说明](releases/0.10.2.md)，远端 CI 结果以对应工作流为准。示例只证明接口能够组合，不测量领域效果，也不包含“把科研判断内化进 PiPilot”。

## 方法与核查附录

### 设计依据

实际采用 `/Users/daidong/Documents/rp-writings/research-ideas/pi-python-core-design/` 下的 2026-10-16 修订，而不是代码仓库中同名的旧副本：

- `DESIGN.md`：“应用直接执行工具”“领域能力包的最小接入约定”。
- `DEVELOPMENT-PLAN.md`：C28、C29。
- `UPSTREAM-COMPATIBILITY.md`：D9。
- `README.md`、`BOOTSTRAP-PROMPT.md`、`DOMAIN-CAPABILITY-PACKS.md`：范围和分工。

现有发布版本已经扩展了早期草案的范围。本次保留已有 Provider、插件、同步接口，以及默认无预算上限、已知取消与显式未知结果的区分，没有按旧草案回退它们。上述实现验证使用未发布的 0.10.1 开发构建，没有升级上游基线。随后经用户确认兼容性方案，安排以 0.10.2 提交、推送和发布；这一授权不构成对所有上层应用的兼容性保证。

### 实现入口

- [Agent API](zh/API.md#应用直接执行工具与能力包组合)：参数、来源、可见性和生命周期合同。
- `src/pi_python/agent.py`：公开入口和运行互斥。
- `src/pi_python/loop.py`、`run.py`：共用执行器、批次预算、运行收尾和历史记录。
- `src/pi_python/messages.py`、`events.py`、`tools.py`：内置消息转换、空轮次事件与调用来源。
- [兼容性测试](../tests/test_application_compatibility.py)、[C28 测试](../tests/test_application_tools.py)、[C29 测试](../tests/test_capability_composition.py)、[验收映射](../compat/COVERAGE.md#d9应用工具批次与能力包最小接入)。
- 能力检查：`require_features(["application-tool-batches-v1"])`。

### 实际执行的检查

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| 新合同及兼容性 | `.venv/bin/pytest -q tests/test_application_tools.py tests/test_application_compatibility.py tests/test_capability_composition.py` | 47 通过 |
| 全量测试 | `.venv/bin/pytest -q -ra` | 798 通过，2 跳过 |
| 静态检查 | `.venv/bin/ruff check src tests examples compat scripts` | 通过 |
| 格式检查 | `.venv/bin/ruff format --check src tests examples compat scripts` | 114 文件通过 |
| 类型检查 | `.venv/bin/mypy src/pi_python` | 43 源文件通过 |
| 补丁检查 | `git diff --check` | 通过 |
| 实际上游对照 | `.venv/bin/python scripts/conformance.py` | 25 组通过，无差异 |
| 构建 | `uv build --out-dir /tmp/pi-python-optional-origin-dist` | sdist、wheel 成功 |
| 独立安装 | 在前一轮建立的独立环境中重新安装本轮 wheel：`uv pip install --python /tmp/pi-python-execute-tools-installed/bin/python --offline --reinstall-package pi-python-core /tmp/pi-python-optional-origin-dist/pi_python_core-0.10.1-py3-none-any.whl` | 成功 |
| 安装包行为 | 从 `/tmp` 使用独立环境运行组合示例和 `pytest -q -o pythonpath= <仓库>/tests/test_application_tools.py <仓库>/tests/test_application_compatibility.py` | 示例输出 `run=completed; domain_passed=False`；43 测试通过 |

独立安装的导入路径确认为 `/tmp/pi-python-execute-tools-installed/lib/python3.14/site-packages/pi_python/__init__.py`，不是源码目录。本轮全量测试和构建原始输出保留在 `/tmp/pi-python-optional-origin-*.log`；这些是临时日志，不是持久化存储保证。

补充清理边界测试时，首版测试在释放工具后立即调用 `aclose()`，此时清理回调尚未执行，触发了既有的 `CleanupTimeoutError`。测试改为有界等待实际清理完成后关闭，并检查迟到的成功不能改写已提交记录；修正后新合同和全量测试均重新通过。
