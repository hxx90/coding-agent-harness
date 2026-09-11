# 历史试跑记录

本目录保存 MiniMax M3 的早期小任务报告，以及 Harness 内部迭代期间的七轮从零建站记录，用于追溯问题发现、归因和工程调整。

这些文件中的 `v0.3.x` 是当次试跑使用的历史内部版本，不是当前对外交付版本。当前对外交付统一为 **MVP 首版（v1.0.0）**。

建议算法首次阅读时跳过本目录；需要核对某个结论或故障归因时再查看：

1. `minimax-zero-code-run-20260806.md`：非标准 Patch；
2. `minimax-zero-code-run-20260807.md`：模型服务 504；
3. `minimax-zero-code-run-20260807-patch-conflict.md`：Patch 冲突与上下文膨胀；
4. `minimax-zero-code-run-20260807-budget-exhausted.md`：预算耗尽与进度误识别；
5. `minimax-zero-code-run-20260807-validation-failed.md`：固定验收和 JavaScript 语法缺口；
6. `minimax-zero-code-run-20260807-helper-loop.md`：辅助脚本空转与熔断；
7. `minimax-zero-code-run-20260807-edit-loop.md`：精确编辑与自动续批次。
