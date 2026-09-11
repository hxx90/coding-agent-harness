# MiniMax 从零建站第四轮：预算耗尽归因

## 1. 运行事实

- 任务 ID：`7dee2e45-ebe1-4ed3-aece-eabdce501b5a`
- Run ID：`ebfed69a-215a-4a24-a629-f4926d8fa580`
- 结果：`budget_exhausted`
- 模型轮次：40/40
- 工具调用：42
- 活跃时间：399.638 秒，未达到 1800 秒时间上限
- 本 Run 实际 Token：Prompt 754,432；Completion 16,238；合计 770,670
- 固定验证：尚未运行

预算是正常生效的止损器，但不是本轮的根因。不能通过继续增加轮次解决。

旧版 `run_finished.usage` 显示的 828,484 是 Task 截至当时的累计值；v0.3.5 已把单 Run 与全 Task 用量分开。

## 2. 现场证据

模型已生成 `index.html`、`styles.css`、20 项 `game-manifest.json` 和约 39 KB 的 `app.js`。按实际代码重新识别，已实现的唯一游戏 ID 为：

1. `minecraft`
2. `snake`
3. `tetris`
4. `minesweeper`
5. `memory`

Harness 每次却都返回 `registered_count=0`。原因是旧识别规则只接受 `registerGame("minecraft", ...)`，没有接受语义等价的 `registerGame(game.id, ...)`。

同时，`app.js` 中出现 4 行 `[HARNESS_COMPACTED ...]`。它们来自旧版上下文压缩投影，不是模型真实生成所需的项目代码，却被模型复制回文件，导致 JavaScript 污染。

## 3. 归因

| 层级 | 结论 |
|---|---|
| Harness | 主因。进度识别漏报；上下文压缩标记可被回写；历史消息随轮次线性增长 |
| 模型 | 放大因素。面对持续的 0/20 反馈后反复搜索、追加测试标记，并复制了上下文摘要 |
| 环境 | 非本轮原因。文件工具均正常执行，没有运行环境错误 |
| 评估 | 尚未进入固定验证，因此不能评价最终网站质量 |
| 预算 | 正常止损，防止错误反馈造成无边界 Token 消耗 |

## 4. OpenCode 对标结论

OpenCode 当前实现会保留近期会话尾部、清理更早的工具输出，并对模型可见的超大工具结果提供有界预览；完整事实另行持久化。它不会把旧工具参数替换成容易被当作源码的代码式占位符。

- [OpenCode Session Compaction](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts)
- [OpenCode Session Runtime](https://github.com/anomalyco/opencode/blob/dev/CONTEXT.md)

本 Harness MVP 不照搬完整会话系统，只采用当前问题需要的最小机制：近期窗口、确定性现场检查点、完整 Trace 保留和工具结果限长。

## 5. v0.3.4 调整

1. 同时识别 literal ID 与 `game.id` 注册方式，并只统计 manifest 内 ID。
2. `write_file` 和 `apply_patch` 禁止新增历史压缩摘要；允许 Patch 删除已经泄漏的摘要行。
3. 长会话只向模型保留最近 6 个 Assistant 工具轮次；较早交互由不含代码的现场检查点替代。
4. 单个工具结果中的大段文本在模型上下文中保留头尾有界预览，完整内容仍在 Trace，模型可按范围重新读取。
5. 现场检查点直接给出当前注册/缺失 ID，并报告源码污染行数。
6. 10 个以上相似模块由每批最多 3 个收紧为最多 2 个。
7. 固定网站验收同步接受 `registerGame(game.id, ...)`，但仍要求 manifest ID、真实交互和最终验证全部通过。

## 6. 验证结果与下一步

- 当前现场重新识别：5/20，缺失 15，污染摘要 4 行。
- 用第四轮最后一次请求回放新上下文投影：历史由 148 条/81,675 字符降为 21 条/9,968 字符；完整 Trace 不变。
- 精确 20 游戏、`game.id`、每批 2 个的确定性闭环通过。
- 全量自动化测试：51/51 通过。
- Python 依赖检查通过。

当前任务可用于验证“保留现场后继续”，但它创建于 v0.3.3，不能作为 v0.3.4 的正式冻结结果。正式交付前仍需用 v0.3.4 全新工作区原样运行一次，并真正进入固定验证。
