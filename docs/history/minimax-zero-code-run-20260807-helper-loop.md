# MiniMax 从零建站第六轮：辅助脚本循环与熔断归因

## 1. 运行事实

- 任务 ID：`7dee2e45-ebe1-4ed3-aece-eabdce501b5a`
- Run ID：`4f634d67-06cc-458b-ba8d-91c263549121`
- 最终状态：`failed`，错误为 `repeated_tool_error`
- 模型轮次：34
- 工具调用：34
- 辅助脚本运行：10
- 活跃时间：174.592 秒
- 本 Run 实际 Token：Prompt 407,245；Completion 4,120；合计 411,365
- 工程修改：0
- 本 Run 验证：0 次

旧 Trace 的 `run_finished.usage=1,694,408` 是整个 Task 当时的累计值，不是本 Run 单独消耗。该历史 Task 没有完整加载 v0.3.5 的用量热更新；v0.3.6 已补齐暂停任务的方法热替换。

## 2. 发生了什么

模型知道旧任务验收要求把 `registerGame(game.id, ...)` 改成字符串 literal，但没有立即编辑。它反复创建 Python 辅助脚本，分析注册调用、游戏 ID、源码位置和占位词。

真实工具序列暴露了三类接口摩擦：

1. 模型多次直觉性地给 `run_helper_script` 同时传入 `name` 和 `source`，希望一次写入并执行；旧工具忽略 `source`，随后返回 `helper_script_not_found`。
2. 模型为每次分析创建新名称，Task 目录达到 10 个临时脚本后，旧工具仍继续出现在 Schema 中。
3. 达到上限后，模型又以 `find_bugs`、`find_bugs2`、`check_placeholder` 三个新名称调用 `write_helper_script`，三次均返回同一“最多创建 10 个”错误，熔断器按规则暂停。

熔断本身符合“同一具体错误连续三次止损”的设计；问题在于 Harness 明知工具不可继续创建，却仍将它暴露给模型，同时没有支持模型已经表现出的单次执行调用方式。

## 3. 项目状态

本 Run 没有改动工程，因此第五轮留下的状态不变：

- 20/20 个游戏 ID 已存在；
- `app.js` 第 1015 行仍包含字面量 `\n`，`node --check` 返回语法错误；
- 当前历史任务的受保护 pytest 仍要求字符串 literal 注册参数；
- 不能判定项目验证通过，也不能据此判定模型不会修复代码。

## 4. OpenCode 对标

OpenCode 的工具注册会根据当前模型和权限筛选可见工具，并在每个循环中重新解析工具集，而不是固定发送全部工具，见其[工具注册实现](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/registry.ts)和[会话循环实现](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt.ts)。

OpenCode 的 doom-loop 阈值同样是 3，但判断条件是工具名和完整输入都相同，见[会话处理器](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/processor.ts)。本 MVP 保留三次止损，同时让 `invalid_tool_call` 按具体错误信息分别计数。

## 5. v0.3.6 调整

1. `run_helper_script` 可选接收 `source`，在一个调用内完成写入和只读运行。
2. 临时脚本仍最多保留 10 个；创建第 11 个时自动淘汰最旧临时文件，不再让整个 Task 永久卡死。
3. 每轮只向模型发送当前状态实际允许的工具；辅助运行预算耗尽后，写入和运行工具都从 Schema 与 Runtime Policy 中移除。
4. 不同 `invalid_tool_call` 原因分别累计，避免把无关参数错误合并成一次重复故障。
5. 上下文检查点直接执行 `node --check`，把当前语法错误和“必须优先修复”反馈给模型。
6. Streamlit 若仍缓存旧 Orchestrator 类，会先按依赖顺序重新加载运行模块；随后为暂停中的历史 Task 热替换消息组装、工具筛选、工具执行和 Agent Loop 方法，确保续跑真正使用新版逻辑。
7. Prompt 明确：简单字符串定位优先使用 `search_code`/`read_file`，复杂一次性分析才使用带 `source` 的辅助脚本。

## 6. 回归结果与下一步

- 单次写入并运行辅助脚本：通过；
- 临时脚本满额淘汰：通过；
- 额度耗尽后动态隐藏工具：通过；
- 不同参数错误独立计数：通过；
- 当前 JavaScript 诊断进入上下文检查点：通过；
- 全量自动化测试：57/57 通过；
- 依赖检查通过；Streamlit 服务健康。

下一步先继续当前保留现场，验证模型能否在新版检查点驱动下修复语法并进入固定验收。该现场跨越多个 Harness 版本，只用于恢复能力验证；恢复成功后，仍需用 v0.3.6 新建工作区做一次无人工干预的正式重跑。
