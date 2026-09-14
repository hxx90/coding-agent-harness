# 两轴代码审查

基线：`e2a2ca0`。范围：基线至当前工作树的 CLI、文档、测试、包配置与旧沙箱修复。规格来源：`devlog/00-scope-and-plan.md` 的 R01–R14。审查发生在首次全量测试 163 项通过之后，说明通过测试不等于没有缺陷。

按 `code-review` 流程，两个独立 Agent 分别做 Standards 与 Spec 审查，均只读、不修改代码。仓库没有额外 AGENTS.md、CONTRIBUTING 或 CODING_STANDARDS；Standards 使用本次架构/接口文档和技能列出的设计坏味道作为判断基线。下面保留两路结论，不混合排序。

## Standards

原报告：**3 项必须修复，1 项判断性建议**。

1. **[P1] 脱敏破坏持久化结构。** 对整个 state 递归字符串替换会修改 ID、路径和 Base64 快照。禁用 MCP 配置 `env.DEBUG="1"` 就可能使普通任务报 invalid_session，违反原始现场与恢复状态的约定。
2. **[P2] 输出关闭后提前杀死合法命令。** stdout/stderr 已关闭但进程还在运行时，固定 `wait(timeout=2)` 无视调用方更长的 timeout，可能提前杀死进程并抛出未转换的异常。
3. **[P2] 授权期间取消后仍能写文件。** SIGINT 只设置 Event，阻塞 input 不退出，授权返回后也没有再次检查 stop。
4. **判断性建议：Duplicated Code。** Shell 和 MCP 分别实现环境过滤，规则已出现差异：`REVIEW_APIKEY` 被 Shell 删除，却被 MCP 继承。

处理结果：

- 持久化结构保留原值；仅对模型/展示文本字段做脱敏。MCP 的非敏感配置值不会自动成为 secret。短敏感值、会话 ID、文件快照和跨重启 undo/redo 加入回归测试。
- 管道关闭后仍按原始 deadline 与 stop 等待真实进程结束。
- POSIX 输入改为可取消读取，所有工具在授权返回后再检查 stop。
- 抽出共用 `command_environment()`，MCP 再叠加用户显式配置的环境变量。

独立复核：原 3 项必须修复与环境过滤建议**全部解决，无对应残余问题**。复核 Agent 运行相关测试 **38 passed in 4.66s**。

## Spec

原报告：功能入口基本覆盖 R01–R14，未发现明确范围膨胀；发现 **3 项实现错误**。

1. **[P1] 文件权限可被路径别名绕过。** R06 要求 allow/ask/deny。允许 write、拒绝 `locked.txt` 后，`./locked.txt` 或绝对路径仍能通过，因为权限检查使用未经规范化的参数。
2. **[P1] 撤销覆盖等待模型期间的用户修改。** R08 要求外部修改冲突保护。原实现按整个用户轮次前后全项目差异记账，纯问答期间的用户编辑会被误当成 Agent 变更，undo 会覆盖。
3. **[P1] 权限提示无法取消，取消后仍可能写入。** R06/R10 要求交互权限和 Ctrl+C。真实 PTY 复现：Ctrl+C 后仍在等待，输入 y 后文件被创建，但终态却显示 cancelled。

处理结果：

- 文件权限 target 使用 Workspace 规范化后的项目相对路径。read/write/edit 各覆盖原始、`./` 和绝对路径三种表示。
- 新增 `ChangeJournal`：文件工具仅记录实际触及文件，Shell/MCP 只在执行窗口记录可见变化。等待模型期间的外部修改不纳入 Agent 撤销；当前工具在崩溃时尚未确认归属的变化禁止自动撤销。
- 取消输入与审批后 stop 检查一并修复；PTY 测试无需再回答提示即可取消，并能继续下一轮。

独立复核：三项原报告问题**全部解决，无对应残余问题**。复核 Agent 运行对应测试 **13 passed**，包含真实 PTY、路径别名和外部编辑保留。

Standards 原 4 项发现，最严重为状态脱敏破坏恢复；Spec 原 3 项发现，最严重为权限绕过与撤销覆盖用户修改。两路所报问题均已修复并经独立复核。

新增集中回归在 `tests/cli/test_review_regressions.py`。额外补齐会话历史损坏、大小限制及 MCP 配置异常检查。最终全量验收见 [04-validation.md](04-validation.md) 与 [evidence/pytest-final.txt](evidence/pytest-final.txt)。
