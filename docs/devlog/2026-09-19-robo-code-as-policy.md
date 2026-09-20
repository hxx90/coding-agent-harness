# 2026-09-19：Robo Code-as-Policy 执行链路

## 背景

此前模型通过多轮 `robo_execute` / `robo_sequence` 调用逐条操控设备，轨迹虽然
有动作和观测，却不是用户期望的 Code as Policies：没有一段可保存、审阅、复现
并在其中闭环观察的代码策略。

## 完成内容

- 新增 `HardwarePolicyRunner`，把模型生成的受限 Python 解释为任务级策略；
- Manifest 动作名可直接调用，并提供 `act`、`observe`、`verify`、`stop`、
  `print`；支持变量、条件、有界循环、算术、比较和下标；
- 整棵 AST 在运动前校验，禁止 import、属性访问、函数/类定义、参数展开及
  文件/网络/进程能力；
- 对源码、AST、语句、循环、动作、输出和总执行时间设置上限；运行错误、取消或
  超时会请求停止设备；
- 每段策略保存为 `hardware-runs/policies/<policy_id>.py`，结果记录源码
  SHA-256、URI、行号、逐步输出、命令回执、最终快照、验证报告和任务结论；
- 新增默认需要审批的 `robo_run_policy`，并从模型工具列表隐藏兼容用的
  `robo_execute` / `robo_sequence`；
- App Server 增加类型化 policy 输入/输出，Textual 硬件卡片展示源码、artifact
  URI 和 observation/action/verification 轨迹；视觉证据继续注入模型上下文；
- Agent 提示和任务完成门禁改为优先在策略中执行 `verify()`，只有
  `policy.task_outcome == "passed"` 才能宣称完成；
- 新增中文改造计划和 CaP/CaP-X 一手资料研究，并更新 README、本地手册与内置
  Skill。

## 安全取舍

- 没有采用原始 CaP 或 CaP-X 的同进程 `exec`；代码由白名单 AST 解释器执行；
- 策略只负责任务层组合，所有动作仍由 `HardwareRuntime` 校验 lease、armed、
  Manifest 和设备状态；实时伺服、限位、watchdog 与物理急停仍属于控制器；
- 模型重试不会回滚已经发生的物理变化，失败后必须按当前观测生成新策略；
- 命令回执、代码无异常和 stdout 都不是成功证明，任务结论只来自独立验证；
- 当前 `robo_run_policy` 是第一阶段的“生成并审批后执行”入口，后续仍需拆分不可变
  preview/execute artifact，并持久化失败、取消、超时和 stop 未确认终态。

## 验证

- 155 项硬件 Runtime、策略、Robo Tool、MuJoCo、App Server、Textual、AgentLoop
  和 CaP-X 聚焦回归全部通过；
- ToolManager 测试确认模型能看到 `robo_run_policy`，看不到 `robo_execute` 与
  `robo_sequence`；
- Pyright 通过，Ruff 全仓检查通过，`git diff --check` 通过；
- 全量 9,319 项 pytest 在约 5% 遇到已有的 ACP/CLI 子进程 10 秒启动超时和
  Connector 级联错误后中止；三个代表性失败串行复跑仍是相同启动/请求超时，
  未涉及硬件策略代码路径；
- 全仓 typos 只报告 `CHANGELOG.md` 中一处既有拼写问题，本次变更文件无新增
  问题。
