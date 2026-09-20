# Robo Code-as-Policy 改造计划

状态：第一阶段已实现，后续阶段按风险逐步开放

## 目标

模型不再把机械臂任务拆成一连串 `robo_execute` Tool Call，而是生成一段可审计、
可复现的 Python 策略。策略在一次执行中完成“观察—计算—动作—再观察—验证”
闭环；每个真实动作仍必须经过 `HardwareRuntime` 的租约、武装、能力白名单、
追踪和停止机制。

这沿用 Code as Policies 的核心思想：模型输出可组合程序，程序通过语义化
perception/control API 与环境交互。它不照搬 CaP/CaP-X 的同进程 `exec`，因为
任意 Python 不是可靠的主机隔离，更不是物理安全边界。

## 目标架构

```text
用户任务
  -> 模型读取 DeviceCapabilityManifest
  -> 生成受限 Python policy
  -> robo_run_policy（审批）
  -> HardwarePolicyRunner（完整 AST 校验 + 有界解释）
  -> observe / Manifest action / verify
  -> HardwareRuntime（lease / arm / limit / trace / stop）
  -> 模拟器或厂商 Adapter
```

CaP-X 保持另一条隔离评测路径：CaP-X worker 执行 benchmark 代码并保存官方
reward/trajectory；Robo Bridge 只作为被测 agent。CaP-X 的 `SimpleExecutor` 不会
进入拥有真机权限的 Robo 进程。

## 本次已经实现

1. 新增 `HardwarePolicyRunner` 深模块：
   - Manifest 动作名直接成为策略函数，同时提供 `act()`、`observe()`、
     `verify()`、`stop()`、`print()`；
   - 支持变量、JSON 值、下标、算术/比较/布尔表达式、条件和有界循环；
   - 禁止 import、属性访问、函数/类定义、文件/网络/进程访问和参数展开；
   - 在任何动作前验证完整 AST；限制源码、AST、语句、循环、动作、输出和总
     运行时间；
   - 错误、取消和超时会请求停止设备；错误包含源码 URI 与行列；
   - 源码以 `<policy_id>.py` 保存，并记录 SHA-256。
2. 新增唯一模型可见动作入口 `robo_run_policy`：
   - 默认需要批准；
   - 返回源码、逐步 observation/action/verification/output、命令回执、最终
     snapshot、验证报告和 `task_outcome`；
   - 把最终视觉证据注入支持视觉的模型上下文；
   - `robo_execute`、`robo_sequence` 保留兼容实现，但 `is_available()` 为 false。
3. App Server 和 Textual 使用类型化 policy 数据：
   - 卡片展示源码、artifact URI、状态、每一步行号和输出；
   - 任务仍只在 `policy.task_outcome == "passed"` 时允许宣称成功。
4. 自动化验证覆盖：
   - 完整折衣代码策略闭环；
   - 末尾包含非法语法时，前面的动作不会执行；
   - 循环超限和运行错误会停止设备；
   - 错误定位、源码持久化、App Server 投影和终端展示。

## 后续计划

### P1：策略预览与不可变审批

把当前一次性的 `robo_run_policy` 拆成 compile/preview 与 execute 两个状态：先展示
源码 hash、动作调用图、最大动作数、预计时长、设备、工作空间、速度/力 envelope
和验证条件；批准绑定不可变 artifact，源码或 Manifest 变化必须重新批准。

验收：用户批准的字节与实际执行的字节一致；Runtime 仍逐动作复验；失败结果
持久化为 `failed/cancelled/timed_out/stop_unconfirmed`，而不是只抛异常。

### P2：结构化物理感知

增加可信 perception service，为策略提供有界的对象检测、位姿、坐标系、深度、
抓取点和力反馈。策略只读取带时间戳、sequence、标定版本、单位和置信度的结构化
结果，不直接打开图片 URI 或导入视觉库。

验收：每个策略判断可追溯到原始 evidence；旧帧、错误坐标系和低置信度结果不能
通过任务验证。

### P3：层级 helper 与技能发布

借鉴 CaP 的层级生成，但只放在执行前：生成 helper 依赖图，拒绝递归和越权调用，
在模拟器/回放/故障注入中验证，然后人工发布带版本和签名的 immutable skill。
真机运行中不动态生成或 `exec` 新函数。

验收：一次偶然成功不能自动晋升技能；技能绑定 Manifest、设备型号、标定版本和
验证记录。

### P4：CaP-X 正式评测

固定 CaP-X commit、任务配置、模型、seed 和 worker 镜像；增加 correlation
manifest，把 CaP-X trial/task/tier/seed/artifact 与 Robo session/run/policy hash/
hardware trace 对齐。先跑 S1 单 trial，再扩展到视觉和多轮 tier。

验收：代码执行成功率与物理任务成功率分开报告；官方 artifact 和 Robo trace 可
双向定位；模型 regeneration 前必须基于当前物理状态重新观察，不能重放旧动作。

### P5：多设备与真机安全

增加跨设备 lease、坐标变换、时钟同步、共享区域互斥和组停止；在 HIL、低速、
限域通过后再接真机。独立控制器 watchdog、联锁、限位和物理急停不由策略代替。

验收：断网、进程崩溃、传感器冻结、设备 NACK 和 stop 未确认均有确定终态，任何
软件路径都不能恢复旧 lease/armed 权限或绕过硬件安全层。

## 设计边界

- `completed` 回执、stdout 和代码无异常都不代表物理任务成功；
- 模型重试不是事务回滚，重试前必须重新观察；
- LLM 不负责毫秒级伺服、碰撞保护或力控；这些必须是可信高层 primitive 下的
  控制器能力；
- simulator reward 不进入模型上下文；任务结论来自独立验证条件和新鲜证据；
- 本地确定性 smoke 不是官方 CaP-X benchmark 成绩。

## 参考

- [Code as Policies 官方项目](https://code-as-policies.github.io/)
- [Code as Policies 论文](https://arxiv.org/abs/2209.07753)
- [Google Research 官方源码](https://github.com/google-research/google-research/tree/master/code_as_policies)
- [CaP-X 官方仓库](https://github.com/capgym/cap-x)
- [详细一手资料研究](research/2026-09-19-code-as-policies-robo.md)
