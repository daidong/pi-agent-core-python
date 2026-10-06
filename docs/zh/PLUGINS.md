# 插件

[English](../PLUGINS.md) | **中文**

需要完整的起步包、离线验证和 wheel 安装步骤时，先读[插件开发流程](PLUGIN_DEVELOPMENT.md)。

插件是给 agent 的一组附加内容，打包成一个有名字的单元：工具、写进系统提示的说明、钩子、技能、提示模板、子 agent 和 MCP 服务器。打包成插件后，可以用 pip 安装，可以在几个项目之间共用，也可以整组打开或关掉。插件不会让 agent 获得原本没有的能力。它只是把你本来要手工写进 `Agent(...)` 的工具、系统提示和钩子组装好，agent 的执行循环本身不变。

格式沿用 Pi 的 package。Pi 的一个 package 对应这里的一个插件，package 里的 TypeScript 扩展代码对应插件的 Python `setup` 函数。技能、提示模板和子 agent 定义用的是和 Pi 相同的 Markdown 文件，大多可以直接拷过来用。

## 使用插件

```python
from pi_python.plugins import load_plugins
from pi_python.providers import AnthropicProvider

async with load_plugins(
    ["hpc-slurm", "./lab-plugin"],                # 一个已安装的插件，一个本地目录
    services={"chooser": ask_in_terminal},        # 插件可以索取的对象
    options={"hpc-slurm": {"partition": "gpu"}},  # 按插件分开的设置
) as plugins:
    agent = plugins.agent(
        provider=AnthropicProvider(api_key="..."),
        model="claude-sonnet-5-5",
        system_prompt="You help run experiments on the cluster.",
    )
    result = await agent.prompt(plugins.expand("/submit run_42"))
```

在普通脚本里，写 `with load_plugins(...) as plugins:`，再调用 `agent.prompt_sync(...)`。

插件来源有四种：

| 来源 | 例子 | 加载什么 |
|---|---|---|
| 已安装插件的名字 | `"hpc-slurm"` | 注册了这个名字的 Python 包（见[发布插件](#发布插件)） |
| 目录路径 | `"./lab-plugin"` | 目录里的 `skills/`、`prompts/`、`agents/`、`mcp.json`，有 `plugin.py` 时也加载它 |
| `.py` 文件路径 | `"./extra.py"` | 这个文件的 `setup(api)`，不找资源目录 |
| `Plugin` 对象 | `Plugin("audit", setup)` | 你在自己代码里定义的插件 |

字符串以 `.` 或 `~` 开头、含有斜杠或以 `.py` 结尾时，按路径处理。插件按给出的顺序加载。打开插件集时，依次运行每个插件的 setup，并连接它们的 MCP 服务器；关闭时断开服务器，并运行插件登记的清理。

并发关闭会等待同一个清理任务；调用方被取消时，先完成清理，再抛出取消异常。回调按注册的逆序执行一次；关闭开始后不再允许组装新的 Agent 或配置。清理回调需要自行结束，框架不隐式设置关闭期限。

启动是一个完整事务：setup 失败、被取消，或警告被提升为异常时，都会释放已注册的资源。启动期间关闭时，先取消并等待 setup（包括其 `finally` 块）退出，再释放资源；关闭后不能重新打开。setup 和清理回调不能关闭其所属插件集。清理回调或其错误报告回调抛出的取消异常，会在剩余清理回调执行后再传播。

`plugins.agent(...)` 接受和 `Agent` 相同的参数，返回的 Agent 把你的配置和插件的贡献合在一起：

- **系统提示：** 先是你的文字，然后是各插件的说明，最后是可用技能的列表。
- **工具：** 先是你的工具，然后是插件的工具和 MCP 工具；有技能时加上 `read_skill`，有子 agent 时加上 `subagent`。两个工具同名时报 `ConfigurationError`。
- **钩子和事件：** 你的钩子先运行，然后是插件的处理函数（见[多个插件处理同一个钩子](#多个插件处理同一个钩子)）。插件的事件监听也会订阅上。

`plugins.expand(text)` 处理人输入的命令。`/skill:name 参数` 插入一个技能的说明，`/模板名 参数` 展开一个提示模板，其他文字原样返回，所以可以对每条用户输入都先调用它再交给 `prompt`。

如果想自己构造 Agent，可以分别取各部分：`plugins.system_prompt(base)`、`plugins.tools`、`plugins.skill_tool()`、`plugins.subagent_tool(tools=..., provider=..., model=...)` 和 `plugins.hooks(你的钩子)`。系统提示里的技能列表会让模型用 `read_skill` 读取技能，所以有技能时要把这个工具加上。

不影响加载的问题会变成 `PluginWarning` 警告，同时保存在 `plugins.diagnostics` 里，例如技能缺少描述、某个 MCP 服务器启动失败。插件的 setup 抛出异常、应用没有提供插件要的服务、两个插件提供同名工具，这几种情况会直接报错并关闭插件集。

加载插件会以你的权限运行它的代码，还可能启动它的 MCP 服务器指定的程序；技能也可以让模型去执行命令。只加载你信任的插件。本库不会自动发现或加载任何插件，只有你的代码点名的插件才会运行。`discover_plugins()` 列出已安装的插件，但不导入它们。

## 编写插件

插件就是一个目录。每一部分都可以没有；只有 `skills/` 的目录也是一个插件。

```text
lab-plugin/
├── plugin.py          # setup(api)
├── ops.py             # plugin.py 用 from .ops import count_duplicates 导入
├── skills/
│   └── dedup-window/SKILL.md
├── prompts/
│   └── dedup.md
├── agents/
│   └── reviewer.md
└── mcp.json
```

```python
# plugin.py
from .ops import count_duplicates

__version__ = "0.1.0"


def setup(api):
    api.add_tool(count_duplicates)
    api.add_system_prompt(f"You work for {api.options.get('lab', 'the lab')}.")

    @api.on("before_tool_call")
    def keep_raw_data(call, args, context):
        # 规则写在代码里执行，而不只写在提示里。
        if call.name == "delete_file" and args["path"].startswith("raw/"):
            return False

    api.add_check(lambda: count_duplicates(["a", "b", "a"]) == 1)
```

`setup` 可以是普通函数，也可以是 `async` 函数。它收到的对象有这些成员：

| 成员 | 作用 |
|---|---|
| `add_tool(tool_or_function)` | 加一个 `Tool`，或一个由 `@tool` 转成工具的函数 |
| `add_system_prompt(text)` | 在系统提示后追加说明 |
| `on(hook, handler)` 或 `@api.on(hook)` | 处理 `Hooks` 的任一槽位，如 `before_tool_call`、`transform_context` |
| `subscribe(listener)` | 接收 agent 的事件 |
| `add_skills(path)`、`add_prompts(path)`、`add_agents(path)` | 加载不在约定目录里的资源 |
| `add_agent(AgentDefinition(...))` | 在代码里定义子 agent，可以给它单独的 Provider |
| `add_mcp_server(name, config)` | 加一个 MCP 服务器，写法和 `mcp.json` 相同 |
| `service(name, default=...)` | 取应用在 `services` 里传入的对象 |
| `options` | 应用的 `options` 里属于这个插件的那一项 |
| `add_check(function, name=None)` | 登记一项自检，供 `await plugins.check()` 运行 |
| `on_close(callback)` | 登记插件集关闭时的清理 |
| `name`、`version`、`root` | 插件名、它的 `__version__`、它的目录 |

相对路径以插件目录为起点。需要应用提供的东西，例如数据库连接、请人做选择的方式，通过 `service` 索取；可调的数值，例如阈值、允许的范围，放进 `options`。这样同一个插件不用改代码就能用在不同项目里。

`await plugins.check()` 运行所有自检，每项返回一个 `CheckResult(plugin, name, passed, detail)`。自检只要没有抛异常、也没有返回 `False`，就算通过。适合放一些快速检查，例如插件的工具在已知输入上是否仍给出预期答案。

### 发布插件

想用 pip 安装插件，就把它做成普通的 Python 包，并在 `pi_python.plugins` 组里注册一个 entry point（入口点，Python 包声明"我提供了什么"的标准方式）：

```toml
# pyproject.toml
[project]
name = "pi-plugin-hpc-slurm"
version = "1.0.0"
dependencies = ["pi-python-core>=0.9"]

[project.entry-points."pi_python.plugins"]
hpc-slurm = "pi_hpc_slurm"
```

入口点的名字就是插件名。它可以指向一个定义了 `setup` 的模块，也可以指向一个 setup 函数（`"pi_hpc_slurm:setup"`）或一个 `Plugin` 对象。包所在的目录就是插件目录，所以 `skills/`、`prompts/`、`agents/` 和 `mcp.json` 放在包里面；hatchling 等构建工具会把它们一起打进 wheel。插件的版本就是包的版本。

## 技能

技能是一个含有 `SKILL.md` 的目录，格式遵循 Pi 实现的 [Agent Skills 规范](https://agentskills.io/specification)：

```markdown
---
name: dedup-window
description: How to choose a time window for merging repeated log events. Use before deduplicating event logs by time.
---

# Choosing a deduplication window

Read `references/semantics.md` for the two grouping rules.
```

系统提示里只列出每个技能的名字、描述和位置，不放具体说明。任务和某个描述吻合时，模型用技能名调用 `read_skill` 读取说明；也可以传一个相对路径 `path`，读取技能目录里的其他文件，但读不到目录以外的东西；每个文件最大 256 KiB。模型是否用某个技能取决于描述，所以描述要写清它做什么、什么时候用。`disable-model-invocation: true` 让模型看不到这个技能，只有人输入 `/skill:name` 时才加载。

查找规则和 Pi 相同。含有 `SKILL.md` 的目录算一个技能，不再往下找；否则，`skills/` 下直接放的 `.md` 文件，以及各子目录里的 `SKILL.md`，都算技能。缺少描述的技能会被跳过并给出警告；名字不合规范只给警告，照样加载。两个插件有同名技能时，用先加载的那个。

## 提示模板

`prompts/` 里的每个 `.md` 文件是一个模板，名字就是文件名。输入 `/dedup events.csv 30` 会展开 `prompts/dedup.md`：

```markdown
---
description: Find duplicate events in a log
argument-hint: <file> [window-seconds]
---
Find duplicate events in $1 using a ${2:-60} second window.
```

| 占位符 | 替换成 |
|---|---|
| `$1`、`$2`、… | 对应的一个参数；没有就是空 |
| `$@`、`$ARGUMENTS` | 全部参数，用空格隔开 |
| `${N:-默认值}` | 第 N 个参数；缺失或为空时用默认值 |
| `${@:N}`、`${@:N:L}` | 从第 N 个起的全部参数，或其中 L 个 |

参数按命令行的方式切分：空格分隔，引号把几个词保持在一起。这些规则都来自 Pi，并且用 Pi 自己的代码做过对照测试。

## 子 agent

`agents/` 里的一个文件定义一个子 agent，主 agent 可以把任务交给它：

```markdown
---
name: reviewer
description: Checks a deduplication result by counting duplicates independently
tools: count_duplicates
model: claude-haiku-4-5
---
You check deduplication results. Report the number only.
```

正文是子 agent 的系统提示。`tools` 写成逗号分隔或 YAML 列表，指定它能用的工具；不写时，它能用主 agent 的全部工具，`subagent` 本身除外。不写 `model` 时，它用调用那一刻主 agent 的模型，包括被钩子切换过的模型。子 agent 还使用主 agent 的 Provider 和全部钩子，包括插件的处理函数。所以取密钥的方式和安全规则对它有效，`prepare_request` 这类每轮都运行的处理函数对它也有效。这和 Pi 一致：Pi 的每个子 agent 是一个加载了同样扩展的 pi 进程。在代码里，`add_agent(AgentDefinition(..., provider=..., options=...))` 可以给它另配 Provider 或选项。

主 agent 只看到一个 `subagent` 工具，它的描述里列出所有可用的子 agent，用法有 Pi 的三种：

| 方式 | 参数 | 结果 |
|---|---|---|
| 单个 | `agent`、`task` | 子 agent 的最终回答 |
| 并行 | `tasks: [{agent, task}, ...]` | 每个回答，以及成功了几个。最多 8 个任务，同时运行 4 个 |
| 接力 | `chain: [{agent, task}, ...]` | 最后一步的回答。任务里的 `{previous}` 换成上一步的回答；有一步失败就停下 |

每个任务都新建一个 Agent，有自己的历史，主对话只看到回答。每次子 agent 运行都带着主 agent 的 `RunLimits`，例如 `max_model_requests`；和 Pi 一样，接力的步数不设上限，所以不可信的文字可能进入模型时，请设好上限。它们的 token 用量加到工具结果的 `usage` 里，每个任务的明细放在 `details` 里。取消主 agent 会中止它的子 agent。Pi 为每个子 agent 启动一个单独的进程；这里子 agent 是同一事件循环里的一个 Agent。

内置子 Agent 工具的三种方式都使用 `ToolContext.run_agent`。外部操作结果未知时，父 Agent 停止并要求人工核对，不会把它降级成普通任务失败。取消或清理超时后，尚未结束的后代任务仍被追踪；这些工作真正结束前，父 Agent 不会报告清理完成，也不能复用。普通子 Agent 失败仍按上述单个、并行和接力规则处理。

## MCP 服务器

`mcp.json` 的格式和 Pi 以及其他 MCP 客户端相同：

```json
{
  "mcpServers": {
    "slurm": {"command": "${PYTHON}", "args": ["${PLUGIN_ROOT}/slurm_server.py"]},
    "docs": {"url": "https://example.org/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}
  }
}
```

写了 `command` 的服务器通过 stdio 启动，可以带 `args`、`env`、`cwd`。写了 `url` 的服务器通过 streamable HTTP 连接，可以带 `headers`。这些字符串里，`${PLUGIN_ROOT}` 是插件目录，`${PYTHON}` 是当前运行的 Python 解释器，其他 `${名字}` 是环境变量。`enabled: false` 保留这一项但不连接。工具名是 `mcp__<服务器>__<工具>`。取自环境变量的值在警告里显示为 `***`；header 或 URL 里含有控制字符时（例如粘贴的 token 末尾多了换行）会被拒绝。HTTP 服务器可以在同一源（协议、主机、端口相同）内重定向；重定向到其他源时不跟随，所以 header 不会被发到那里。

`mcp.json` 里写错的一项，例如用了没有设置的环境变量，会被跳过并给出警告；同样的错误出现在 `add_mcp_server` 里时，会报 `ConfigurationError`。启动或连接失败的服务器也会被跳过并给出警告，其他内容照常加载。本库不支持的设置，如 `timeout`、`exposure`、`oauth`，会被忽略并给出警告；超时请用 `RunLimits(tool_timeout=...)`。和 Pi 一样，不接受 SSE 传输方式。使用 MCP 需要 `pip install 'pi-python-core[mcp]'`。不用插件时，`pi_python.mcp.connect_http(url, headers=...)` 连接 HTTP 服务器，用法和启动本地服务器的 `connect_stdio` 一样。

在 POSIX 系统上，stdio 配置还接受 `"process_scope": true`，适用于 `mcp.json`
和 `api.add_mcp_server`。连接关闭时会清理残留进程组及继承归属的嵌套 pi-python
stdio 连接。该字段必须是布尔值，HTTP 配置不接受它。
取消行为和平台限制见 [进程归属](MCP_INTERACTION.md)。

两种传输方式都支持 `call_metadata`。它是一个 JSON 对象，作为 MCP 工具请求的
`_meta` 发送，不会进入工具 schema 或模型填写的参数。运行上下文通过插件 options 传入：

```python
def setup(api):
    api.add_mcp_server("sandbox", {
        "url": api.options["url"],
        "call_metadata": {"context_id": api.options["context_id"]},
    })
```

应用向 `load_plugins` 传入 `options={"sandbox-plugin": {"url": sandbox_url, "context_id": task_id}}`。
注册服务器时保存元数据副本，每次调用再独立复制。字符串原样保留，不展开环境变量。
框架不解释 `context_id`。不同任务上下文应分别加载插件集；注册后修改 options
不会改变已有连接的上下文。`mcp.json` 也接受同名字段。不使用插件时，向
`connect_stdio`、`connect_http` 或 `mcp_tools` 传入 `call_metadata` 即可。
进度标识和通知仍由 SDK 管理。如果 SDK 的 `ClientSession.call_tool` 不支持 `meta`，
配置元数据（包括空字典 `{}`）会抛出 `ConfigurationError` 并终止加载。
未配置元数据时仍兼容旧 SDK。

## 严格加载与就绪检查

MCP 服务还可以请求宿主模型和用户表单。宿主通过
`load_plugins(..., mcp_callbacks={(插件名, 服务名): MCPCallbacks(...)})` 授权。
服务 JSON 可以声明 `required_capabilities`，但不能放回调对象或自行获得能力。
详见 [MCP 交互](MCP_INTERACTION.md#插件授权和就绪检查)。

`load_plugins(..., strict=True)` 将任何加载诊断视为失败，包括插件资源被跳过、
配置项不受支持，以及 MCP 连接失败。失败时抛出 `ConfigurationError`，
关闭已启动的连接并执行插件的资源清理。默认仍允许带警告加载。
严格加载不会自动运行自检，也不会猜测应用需要哪些能力。

使用 `readiness()` 执行已登记的自检，并核对明确要求的能力：

```python
async with load_plugins(["sandbox-plugin"], options=options, strict=True) as plugins:
    status = await plugins.readiness(
        required_tools=["mcp__sandbox__execute"],
        required_skills=["sandbox-rules"],
        required_mcp_servers=["sandbox"],
        required_checks=[("sandbox-plugin", "health")],
    )
    status.require_ready()  # 不满足要求时，抛出 ConfigurationError 并列出原因
    agent = plugins.agent(provider=provider)
```

返回的 `ReadinessResult` 分别记录是否已加载（`loaded`）、缺少的工具/技能/MCP 服务、
缺少的自检，以及实际执行的自检结果。自检用 `(插件名, 自检名)` 标识。
与 `all(c.passed for c in await plugins.check())` 不同，要求的自检没有登记时，
`ready` 为假。如果没有要求任何自检，空自检列表可以通过；已登记的自检仍全部执行，
任何一项失败都会使 `ready` 为假。

工具要求检查插件提供的工具，包括自动生成的 `read_skill` 和 `subagent`，不包括后来
另外传给 `Agent` 的工具。MCP 服务必须当前已连接；被禁用或连接失败的配置不算存在。
每次 `readiness()` 都会并发 ping MCP 会话，每个服务最多等待 `mcp_timeout` 秒（默认 5 秒）。
失败或超时的服务及其能力不计入本次快照；后续 ping 成功时恢复可用状态。
这是检查时的快照，不是持续监控；ping 只验证协议响应，业务功能仍须登记相应自检。
宽松模式下，诊断信息会随结果返回，但本身不会使就绪检查失败。
在上下文管理器内部调用 `require_ready()`，失败退出时会关闭资源。
插件集打开前或关闭后，`loaded` 和 `ready` 都为假。

## 多个插件处理同一个钩子

几个插件可以处理同一个钩子。处理函数的运行顺序是：你自己的钩子，然后按加载顺序运行各插件的，同一插件内按登记顺序。结果的合并方式和 Pi 的扩展运行器相同：

| 钩子 | 结果怎样合并 |
|---|---|
| `before_tool_call` | 第一个拦截（返回 `False`）或给出 `ToolResult` 的处理函数说了算，后面的不再调用 |
| `after_tool_call`、`on_payload` | 依次传递：每个处理函数看到的是前面的处理函数改过之后的结果 |
| `transform_context`、`convert_to_llm` | 依次传递：每个处理函数接收上一个返回的消息，返回 `None` 表示不改 |
| `prepare_request`、`prepare_next_turn` | 每个处理函数看到前面的处理函数设置的模型、选项、工具和上下文；它们新加的消息在全部运行完后按顺序追加 |
| `finish_turn` | 全部运行；`"end"` 优先于 `"continue"` |
| `get_api_key` | 用第一个返回的密钥 |
| `on_response`、`on_provider_stream_event`、事件监听 | 全部运行 |

插件的处理函数抛出异常或返回了错误的类型时，一份 `PluginFailure` 报告交给 `load_plugins` 的 `on_error` 回调；回调可以是普通函数或 async 函数，默认写进日志。这个处理函数的结果被跳过，其他处理函数照常运行，所以一个有问题的插件不会让 agent 停下。`before_tool_call` 例外：和 Pi 一样，那里的错误会让这次工具调用失败。你自己的钩子保持核心原来的行为，见 [API](API.md#钩子)。

## 与 Pi 的差别

Pi 的命令行程序负责查找和加载 package；本库没有应用程序，所以要加载哪些插件由你的代码指定。其他差别：

- 模型用 `read_skill` 工具读取技能，因为本库没有通用的读文件工具。
- 子 agent 在同一进程里运行，而不是各自一个进程。
- 不读取技能目录里的 `.gitignore` 文件。
- 文件开头的元数据（frontmatter）由一个内置的小解析器读取，只支持这些文件用到的那部分 YAML。超过 64 KiB 或嵌套超过 64 层的 frontmatter 不予读取，并对这个文件给出警告。在大约 2000 个真实的技能、子 agent 和命令文件上，它的结果和 Pi 用的 YAML 库一致；它还接受含有 `: ` 却没加引号的描述，YAML 会拒绝这种写法。
- MCP 服务器不支持 OAuth、工具暴露模式、单次请求超时和 `!命令` 形式的值。

逐项对照见 [验收映射](../../compat/COVERAGE.md)。完整的插件示例在 [examples/plugins/lab_tools](../../examples/plugins/lab_tools)，[examples/plugin_demo.py](../../examples/plugin_demo.py) 可以离线运行它。


## 声明框架能力要求

目录插件可以添加 `pi-plugin.json`：

```json
{"requires": ["plugin-requires-v1", "task-scope-v1", "loop-portal-v1"]}
```

加载器在导入 `plugin.py` 前检查此文件。能力不足时会在插件导入和初始化前拒绝加载。
文件只接受 `requires` 字段，值为非空字符串集合。严格或普通加载都会拒绝未知能力。
不声明要求的旧插件仍可加载。

代码插件可以写 `Plugin("example", setup, requires=("task-scope-v1",))`。
单个 Python 文件、入口点模块或函数可以声明 `__requires__`。
这些 Python 声明在导入后、任何插件初始化前检查；需要导入前检查时使用目录声明。
根目录声明与 Python 声明会合并，归一化结果保存在 `Plugin.requires`。
旧框架可能忽略声明文件，因此安装约束仍应选择提供 `plugin-requires-v1` 的构建。

`api.task_scope()` 创建任务作用域并自动注册关闭回调。
先注册任务依赖资源的关闭回调，再创建作用域，因为关闭按注册顺序逆序执行。
不要在受管任务中关闭插件。作用域不取得宿主 Provider 或事件循环的所有权。
具体调用方式见 [API 文档](API.md)。
