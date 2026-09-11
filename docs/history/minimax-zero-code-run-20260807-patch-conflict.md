# MiniMax M3 第三轮从零建站试跑与 Patch Conflict 归因

| 项目 | 内容 |
| --- | --- |
| 执行时间 | 2026-08-07 10:22–10:25（Asia/Shanghai） |
| Harness | v0.3.2 |
| 最终状态 | `failed / repeated_tool_error / patch_conflict` |
| 是否进入固定验收 | 否 |

## 1. 本轮进展

v0.3.2 的分块规则生效。MiniMax 依次成功完成：

- 创建 `index.html`、`styles.css` 和 20 项 `game-manifest.json`；
- 用 `write_file replace` 创建 4,787 字符的 `app.js` 核心；
- 用 `write_file append` 追加 7,578 字符的第一批游戏代码；
- 最终文件中实际注册了贪吃蛇和俄罗斯方块两款游戏。

核心通过 `window.Hub` 暴露 `registerGame`，追加批次在核心 IIFE 外通过 `window.Hub` 注册游戏。这一结构在 JavaScript 中合法。但模型误判追加模块无法访问注册器，转而尝试把模块移回核心闭包。

三次 `apply_patch` 均使用了与最新文件不一致的 hunk 起始行或上下文；其中 append 边界缺少换行，使 `})();// 游戏注释` 连在同一行，进一步增加了模型误判。三次冲突后熔断器暂停。

运行统计：22 Turn、24 次工具调用、183.722 秒；Prompt 292,815、Completion 9,610、总计 302,425 Token。未执行 `run_validation`。

## 2. 归因

| 层级 | 判断 | 说明 |
| --- | --- | --- |
| 模型 | 主触发原因 | 将合法的 `window.Hub` 外部模块结构误判为闭包访问错误，并反复构造不匹配 Patch |
| Harness | 明显放大因素 | append 不自动补换行；工具结果不反馈注册进度/合法架构；Patch 只信任声明行号；历史大段写入反复进入上下文 |
| 环境 | 非主因 | 模型服务、文件写入和隔离环境正常 |
| 评估 | 尚未发生 | 未运行固定测试，不能判断最终产品质量 |

## 3. v0.3.3 调整

- append 在前后都缺换行时自动插入一个换行；
- 20 游戏任务明确推荐 `window.GameHub/Hub` 公共 API + 闭包外独立游戏模块，并说明不应移回核心闭包；
- 每次写入 `app.js` 后返回已注册数量、已注册 ID、缺失 ID 和下一步 append 建议；
- 检测到 `window.Hub/GameHub` 时明确返回该架构合法；
- Patch 在声明行号偏差不超过 100 行且旧上下文唯一匹配时自动重定位；
- Patch 冲突返回结构化 `write_file append` 恢复建议和累计次数；
- 成功的大段 mutation 参数在后续模型上下文中替换为哈希摘要，需要核对时重新 `read_file`；完整内容仍保留在 Trace；
- 任一成功工程写入会清空此前累计的写工具错误。

## 4. 下一步

用 v0.3.3 全新运行同一任务。重点观察模型是否根据 `website_progress.missing_ids` 持续 append，而不再搜索、重构已成功核心；同时比较 Prompt Token 增长是否明显下降。
