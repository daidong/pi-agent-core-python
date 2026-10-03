# 固定上游参照

参照为 Pi `v1.0.0`，提交 `a13d35a742c6ef8462812a28fbe1d8c8b7431c32`。`runner.ts` 导入并实际实例化该提交的 `Agent`，只注入模拟流、假工具、钩子和队列操作。它不重写执行循环。

```bash
uv sync --locked
uv run python scripts/bootstrap_reference.py
node --import ./reference/register.mjs reference/runner.ts compat/fixtures/C01-text.json
uv run python scripts/conformance.py
uv run python scripts/verify.py
uv run python scripts/linux_verify.py
```

参照需要 Node 24.4.1（本次实际使用），上游声明最低 22.19.0。`register.mjs` 只把包导入定位到固定源码。Node 不进入 Python 包运行依赖。`bootstrap_reference.py` 核查 tag、21 个源码摘要，使用固定源码里的 `package-lock.json` 执行 `npm ci --ignore-scripts`。

上游源码包不含生成的模型目录，完整编译最初失败。开发时使用上游 `hydrate-model-data` 成功生成数据后，将它归档为 `model-data.tar.gz`，摘要见 `model-data.json`。重建使用归档，不重新获取浮动模型目录。它只用于上游现有单元测试的模型元数据；所有差分都使用模拟 Provider，不请求真实模型。

`pi/` 与安装依赖由重建脚本生成并被 Git 忽略。锁文件摘要保存在 `lock-manifest.json`。上游 MIT 版权保存在根目录 LICENSE 和 NOTICE，也包含在 wheel 中。生成目录的归档不表示整个上游应用被移植。

## 比较规则

共享输入位于 `compat/fixtures/`。输入包含脚本模型响应、工具结果、执行模式、钩子与事件触发的排队操作或取消。响应可以先流出文本再失败或等待取消；参照运行器在取消信号已触发时像真实 Provider 一样立即返回 aborted 回答，不记为一次请求。并发完成顺序用事件屏障安排，不依赖短暂 sleep。每个实际输出位于 `compat/results/<fixture>.upstream.json` 和 `.python.json`；`conformance.json` 记录逐项命令、退出码与比较结果。

比较保留每次模型请求、模型标识、工具实际参数和次数、事件顺序、完整消息及运行状态。允许的转换是字段命名、去除时间与随机 ID、用户纯文本块与字符串之间的等价表示、明确识别的错误类别，以及流式回答开始时 `message_start` 的结束原因（上游此时为 `stop`，Python 为 `pending`）。流中的部分快照不比较，提交的消息逐项比较。错误映射保留调用 ID、工具名和输入相关错误文字；未识别的错误不会被泛化成同一文本。实现见 `compat/runner.py::map_errors`。

对照输入与结果分开维护。`scripts/make_fixtures.py` 只生成输入；预期输出必须来自 `runner.ts` 的真实运行。`pytest` 使用已保留的上游输出比较当前 Python 代码，Linux 安装验证因此不需要 Node。

## 候选版本接收

接收程序不自动轮询，也不自动更新基线：

```bash
uv run python scripts/check_candidate.py candidate.json
```

程序先按设计 schema 验证数据，再独立获取官方 release、解析远端 tag，并按固定提交下载前后源码。它重新计算完整文件差异与 SHA-256。通过形状校验不等于通过来源核查。路径、重复项、过期基线、移动 tag、摘要和增删改名的对应关系均被检查；release notes 仅作为字符串数据。

纯校验函数为 `compat.release.validate_candidate`。其输入 `release`、`resolved_commit`、`before` 和 `after` 必须由独立获取的证据提供，不能直接复用候选内的声明。此模块和接收脚本属于开发工具，不随 `pi_python` 导入。

本次升级接口验收包括真实基线的无需操作候选，以及测试中明确标识的合成升级。合成测试验证新增、修改、删除、改名与拒绝分支；不宣称已经适配基线之后的真实版本。真正更新基线仍应按设计完成新旧参照比较、Python 回归及安装检查。

## 新增 Provider 协议对照

`provider-runner.ts` 直接导入固定源码中的 Anthropic/OpenAI `stream` 和 agent 的 `streamProxy`。通过可注入 fetch 接收 `compat/provider-fixtures` 中同一份 SSE 数据，再用 `scripts/provider_conformance.py` 与 Python 比较最终内容、签名和停止原因。签名不通过解析 JSON 来放宽比较，序列化字符串也须一致。

```bash
uv sync --locked --extra providers
uv run python scripts/provider_conformance.py
```

新增测试不请求外部模型；真实 OAuth 和付费模型调用尚待账号联调。
