# MiniMax M3 第二轮从零建站试跑与 504 归因

| 项目 | 内容 |
| --- | --- |
| 执行时间 | 2026-08-07 09:44–09:49（Asia/Shanghai） |
| Harness | v0.3.1 |
| 模型 | MiniMax-M3，OpenAI-compatible Adapter |
| 最终状态 | `failed / model_service_error / HTTP 504` |
| 是否进入自动验收 | 否 |
| 是否可判断完整任务完成 | 否 |

## 1. 本轮进展

本轮证明 v0.3.1 的大文件写入修复已经生效：MiniMax 连续使用 `write_file` 成功创建了以下产品文件：

- `index.html`：2,440 bytes；
- `styles.css`：11,697 bytes；
- `game-manifest.json`：5,761 bytes，包含 20 项游戏清单。

随后模型请求准备继续生成 `app.js`。该请求约 185 秒后由上游网关返回 504 页面。Adapter 在一个 Harness `model_request` 内部已按既有策略最多尝试 3 次，因此继续增加相同重试不能解决单次生成过重的问题，反而可能增加等待与计费。

运行统计：6 Turn、8 次工具调用、272.91 秒；已确认 Usage 为 Prompt 35,170、Completion 6,952、合计 42,122 Token。由于 `app.js` 尚未生成，本轮未运行固定验收。

## 2. 归因

| 层级 | 判断 | 说明 |
| --- | --- | --- |
| 模型服务 | 主因 | 上游网关返回 HTTP 504，响应内容为 temporarily unavailable |
| 任务/输出形态 | 主要放大因素 | 下一步需要生成包含 20 款游戏逻辑的大型 `app.js`，单次响应可能过长 |
| Harness | 次要改进项 | 已有 3 次重试，但未限制单次修改参数规模，也未强制模型分批生成 |
| 环境 | 非主因 | 前三次模型请求、写文件、隔离工作区均正常 |
| 评估 | 尚未发生 | 未执行固定验收，不能判断实现是否合格 |

## 3. v0.3.2 调整

- `apply_patch.patch` 和 `write_file.content` 单次最多 12,000 字符；
- Tool Schema 同步声明 `maxLength`，在模型生成前提供约束信号；
- System Prompt 要求先 `replace` 核心框架，再用 `append` 分批追加；
- 对 10 个以上相似模块，每批最多实现 3 个；
- 20 游戏任务的只读 `TASK.md` 重复写入相同分批要求；
- 保留原有最多 3 次服务重试，并在最终错误中显示实际尝试次数。

这些调整不会减少 20 个游戏的最终验收要求，只改变生成节奏。

## 4. 下一步

使用 v0.3.2 和全新工作区再次原样提交任务。预期 Trace 中应看到：`app.js` 核心 `replace`，随后多次不超过 12,000 字符的 `append`，最后进入 `run_validation`。若仍在小块输出时出现 504，则优先归为服务稳定性，而不是继续修改任务或放宽验收。
