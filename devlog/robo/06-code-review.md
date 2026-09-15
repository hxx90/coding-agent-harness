# Robo 两路独立审查

固定点：CLI 2.0.0 `4cc205b9aaa7923600199112092eb5d5bbc0cd32`；首轮实现提交 `36cc451`。比较 `git diff 4cc205b...HEAD`，并对修复的工作区增量独立复核。用户提供的两份产品/技术文档为 spec，仓库既有模式和 code-review 技能基线为 Standards；无另设 issue tracker，直接使用用户给定文档，不需要中断实现询问。

## Standards

初审 3 项：

1. P1：宿主 SIGKILL 后 sleeping worker 孤立，墙钟监管失效。
2. P2：同步 socket 等待不能及时响应 session.stop 或模型预算。
3. P2：events 等待循环为已经断开的客户端自动续租。

分别用专属存活 FD、可取消异步 Unix socket、按真实请求续租修复。独立复核原三项无残余；针对性测试 **5 passed in 14.44s**。复制式虚拟环境 launcher 的关联兼容路径另验证 **3 passed in 0.66s**，文件/网络/子进程限制仍有效。没有新增阻塞性的代码坏味道建议。

## Spec

初审 3 项：

1. P2：每次推理之前缺少最新现场/job 摘要，只有主动查询工具。
2. P1：同上，宿主死后 worker 遗留，资源限制失效。
3. P1：未知动作只阻止本 job；新 job 取得控制权前没有核对旧动作影响。

补充带采样时间、job/version/status/event_cursor 的有界宿主摘要；增加工作进程死亡监视；在终结和重启时自动查询/观察核对，并在任何旧动作仍 unknown/dispatching 时阻止新 job。

首次复核 **3 passed**，但发现“提交回执丢失→lookup恢复”没有登记摘要 anchor。已抽出 remember_job 同时供正常结果与恢复使用，并验证恢复后真实的下一次模型请求包含原 job 的最新状态、版本和游标。最后独立复核 **1 passed**，原三项均解决。

两轴各初审 3 项，最严重均为 P1；均已修复且独立复核无残余。两路报告独立保留，不把重复的宿主死亡问题合并计算。未将已明确标出的 MHS/真实硬件/模型凭据阻塞当作已完成集成。
