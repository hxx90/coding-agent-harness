# Code as Policies / CaP-X 用于 Robo Hardware Harness 的研究

日期：2026-09-19

证据范围：只采用 Code as Policies（CaP）论文、官方项目与官方源码，CaP-X 论文与固定提交 `53e9966d7a8e2fa7494676772bccc35280f5c0ed`，以及本仓库当前源码。文中分别标记“官方事实”“当前实现事实”和“设计推论”，避免把论文实验、基准设施和产品安全承诺混为一谈。

## 结论先行

1. **CaP 适合成为 Robo 的任务策略表示与生成方法，不适合成为硬件安全边界。** 它的价值是让模型生成可组合的 Python 程序，在程序中读取感知结果、调用控制 primitive，并用 `if`、`for`、`while` 形成闭环；它不替代伺服器、轨迹控制器、限位、看门狗、联锁或物理急停。
2. **Robo 当前已经具备正确的执行底座。** `HardwareRuntime` 掌握设备发现、租约、武装、能力 allowlist、审批、动作回执、停止确认、观测、验证和 Trace；新出现的 `HardwarePolicyRunner` 还提供受限 AST 解释、循环与动作上限、总超时、失败后停止和源码留档。这比原始 CaP 和 CaP-X 的同进程 `exec` 更适合真机方向。
3. **当前最重要的缺口不再是“有没有策略执行器”，而是策略如何安全地产生、批准、观测和验证。** `HardwarePolicyRunner` 已接入模型可用的 `robo_run_policy`、App Server 与 Textual UI；仍没有预执行风险预览、CaP 的层级 helper 生成、经过验证的技能库、结构化 RGB-D/坐标系感知、多设备编排、任务级恢复和完整失败状态持久化。
4. **CaP-X 应先作为隔离的 benchmark 与 agent 协议使用。** Robo 已有 OpenAI-compatible Bridge，可作为 CaP-X 被测 agent；CaP-X worker 继续负责环境、代码执行和官方轨迹。不要把 CaP-X 的 `SimpleExecutor` 引入 Robo 主进程，更不要让它直接控制真机。
5. **推荐双路径落地。** 路径 A 用固定版本 CaP-X 衡量 Robo 的代码生成与闭环修复能力；路径 B 把通过静态检查、仿真验证和人工批准的受限策略交给 `HardwarePolicyRunner`，所有动作仍逐个经过 `HardwareRuntime`。两条路径共享策略、观测和轨迹语义，但不共享不可信执行边界。

一句话架构是：**模型可以写策略，策略解释器可以编排 primitive，只有 `HardwareRuntime` 和独立安全控制器可以授权并驱动物理设备。**

## 一、CaP 到底是什么

### 1.1 语言模型程序，而不是动作列表

**官方事实。** CaP 把语言模型当作 Language Model Program（LMP）生成器：输入自然语言任务和包含机器人 API 的 prompt，输出 Python 程序；程序读取感知 API 的结果，计算控制参数，再调用控制 API。论文把这种方式与直接预测自然语言动作或固定技能序列区分开，因为生成代码可以使用变量、算术、第三方库、条件和循环。[论文 Method](https://arxiv.org/html/2209.07753#S3) [官方项目页](https://code-as-policies.github.io/)

典型数据流是：

```text
用户目标
  -> Hints（API、imports、类型/约定）+ Examples（指令—代码示例）
  -> LMP 生成顶层 Python
  -> 补齐未定义 helper
  -> 程序反复 observe -> compute -> act
  -> 环境或机器人产生新状态
```

因此 CaP 最值得 Robo 借鉴的不是“让模型输出 Python”这一表面形式，而是三个组合：

- 用**小而有语义的感知与控制 API**限定模型能表达的动作；
- 用**程序控制流**表达长任务和闭环，而不是让模型逐 token 参与实时控制；
- 用**层级生成**把一次提示中没有现成实现的 helper 拆成可复用函数。

### 1.2 Hints、Examples 与会话历史

**官方事实。** CaP prompt 由 Hints 和 Examples 组成。Hints 告诉模型可用 API、imports 和语言约束；Examples 提供自然语言到代码的 few-shot 映射。Interactive Demo 还会把先前指令和生成代码追加到当前会话，使后续的 “undo that” 等引用有上下文。论文附录强调 Examples 必须无 bug、命名和类型约定一致，并需要谨慎的 prompt engineering。[论文 Prompt Structure](https://arxiv.org/html/2209.07753#S3.SS1) [论文 Prompt Design](https://arxiv.org/html/2209.07753#A0.SS1) [Interactive Demo](https://github.com/google-research/google-research/blob/4700efb9afa54286b0e04473ba80a13e8461e25f/code_as_policies/Interactive_Demo.ipynb)

**对 Robo 的设计推论。** `DeviceCapabilityManifest` 里的 `description + JSON Schema` 是类型化调用的良好基础，但还不足以稳定生成 CaP 策略。策略 prompt 应由版本化 manifest 编译而来，至少包含：

- 函数签名、参数单位、坐标系和返回值；
- 前置条件、后置条件、失败模式和是否幂等；
- 每个 primitive 的速度、力、工作空间和持续时间上限；
- 少量经验证的成功示例，以及明确的禁止示例；
- `observe`、`verify`、`stop` 的语义，尤其是“命令完成不等于任务成功”。

示例应绑定设备型号、manifest 版本、标定版本和验证结果；不能把某次真机成功代码未经复核就自动加入 prompt。

### 1.3 层级代码生成

**官方事实。** CaP 先生成顶层代码，再解析 AST，寻找当前 scope 中未定义的函数。独立的 function-generator LMP 根据函数名和上下文生成实现；系统递归扫描新函数体，深度优先补齐更低层 helper，并把已生成函数加入后续可用 scope。官方示例展示了这种递归函数生成，而不是要求单次补全写出全部细节。[论文 Hierarchical Code Generation](https://arxiv.org/html/2209.07753#S3.SS3) [官方 fgen prompt](https://code-as-policies.github.io/prompts/fgen.txt) [生成结果示例](https://code-as-policies.github.io/codegen/blocks/1.txt)

**当前实现事实。** `HardwarePolicyRunner` 有意不允许 `FunctionDef`、属性访问和 import，也没有动态补齐未定义函数。这是正确的真机默认值，而不是需要直接解除的限制。

**对 Robo 的设计推论。** 层级生成应放在执行前的离线编译阶段：

1. 模型生成顶层策略和候选 helper；
2. 解析完整依赖图，拒绝递归、动态分派、未绑定名称和越权 API；
3. 把 helper 降低为当前受限 AST 能解释的形式，或调用经过签名发布的可信技能；
4. 运行静态边界检查、确定性模拟、故障注入和任务后置条件验证；
5. 由人工审阅并发布不可变的 skill artifact；
6. 真机只加载已发布版本，不在运动过程中临时生成新函数。

这样保留 CaP 的组合能力，同时不把任意函数生成变成新的真机代码执行入口。

## 二、感知 API、控制 API 与执行闭环

### 2.1 感知 API 应返回可计算的结构，而不只是图片

**官方事实。** CaP 的程序把感知结果当作普通 Python 值进行几何和逻辑计算；论文实验使用对象检测、位置、边界框等环境 API。官方 reactive controller 示例在循环中重新读取环境状态，再决定下一步控制。[论文 Experiments](https://arxiv.org/html/2209.07753#S4) [Reactive Controllers](https://github.com/google-research/google-research/blob/4700efb9afa54286b0e04473ba80a13e8461e25f/code_as_policies/Experiment_%20Reactive%20Controllers%20on%20Toy%20Tasks.ipynb)

**当前实现事实。** `robo_observe` 已返回有界 `DeviceSnapshot`，并能携带 RGB、depth、point cloud、thermal 类型的 evidence 元数据；UI/模型可获得受信图片。策略内的 `observe()` 目前只得到 snapshot 的 JSON 表示，evidence URI 本身不能安全地变成对象、分割、抓取点或三维位姿。

**对 Robo 的设计推论。** 需要可信 perception service 把原始 evidence 转成有界语义结果，而不是让策略自行打开 URI、import 视觉库或访问文件系统。建议至少提供：

```text
observe_frame(camera, modalities) -> frame_ref
detect_objects(frame_ref, classes) -> bounded detections
estimate_pose(frame_ref, detection_id, target_frame) -> pose + covariance
segment(frame_ref, detection_id) -> mask_ref
query_workspace(device) -> bounds + occupied regions
read_force(device) -> value + unit + timestamp
```

所有结果必须带 `source_id`、采集时间、sequence、frame、标定版本、单位、置信度/协方差和过期时间。图像、点云和视频保存在模型上下文之外；策略只接收有大小上限的引用和语义摘要。

### 2.2 控制 API 是高层 primitive，不是实时控制环

**官方事实。** CaP 在真实 UR5e、桌面抓放和 Everyday Robots 移动操作平台上调用既有控制 primitive；生成程序负责参数化和组合它们。论文也明确指出能力受感知/控制 API 范围约束，复杂连续控制与执行后调参尚未解决。[论文 Physical Experiments](https://arxiv.org/html/2209.07753#S4) [论文 Discussion](https://arxiv.org/html/2209.07753#S5) [Reactive Controller 附录](https://arxiv.org/html/2209.07753#A0.SS6)

CaP-X 同样把 contact-rich manipulation、紧密视觉伺服和碰撞规避列为薄弱点，并讨论让 CaP 负责高层任务/恢复、让 VLA 或优化控制承担低层连续控制的混合方向。这进一步支持把 Robo policy loop 放在任务层，而非实时控制层。[CaP-X Limitations](https://arxiv.org/html/2603.22435v2#A1)

**对 Robo 的设计推论。** 策略可以调用 `move_to_pose`、`grasp`、`fold_cloth`、`conveyor_start` 之类的有界 primitive；毫秒级速度环、关节插补、碰撞保护和力控必须留在设备控制器或可信 adapter。若任务需要连续视觉伺服，应提供一个经过验证、带 deadline 和 envelope 的控制 primitive，而不是让 LLM 生成高频 `while` 循环。

### 2.3 真正的闭环是 observe—act—verify—recover

CaP 允许程序在一次执行内用 `while/if/for` 多次调用感知和动作 API，这比一次性动作序列更适合柔性任务。但循环存在不代表任务已经闭环。Robo 还必须定义：

- 什么观测足以进入下一步；
- 命令回执如何与物理后置条件分离；
- `failed`、`inconclusive`、过期证据和物理状态未知时如何恢复；
- 哪些恢复可以自动执行，哪些必须停止并请人处理。

当前 `robo_verify` 和 `HardwarePolicyRunner.verify()` 已经把动作完成与任务成功分开，并检查验证证据的新鲜度/sequence，这是比原始 CaP demo 更强、应继续保留的语义。

## 三、受限执行与物理任务验证

### 3.1 官方执行器不是安全沙箱

**官方事实。** 原始 CaP 的 `exec_safe` 通过字符串检查拒绝 `import` 和 `__`，把 `exec`/`eval` 替换为空操作，然后仍在同一 Python 进程调用 `exec`。这是一层 demo 防护，不是可证明的隔离边界。[Interactive Demo 的 `exec_safe`](https://github.com/google-research/google-research/blob/4700efb9afa54286b0e04473ba80a13e8461e25f/code_as_policies/Interactive_Demo.ipynb)

CaP-X 固定提交的 `SimpleExecutor` 更明确地允许 arbitrary imports，并在环境进程内执行代码；官方开发说明称其为 “safe-ish”，建议需要更强隔离时使用 Docker 或 nsjail。[CaP-X executor](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/tasks/base.py#L58-L82) [CaP-X known issues](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/development.md#L137-L140)

**结论。** 两者都不能在持有真机凭据、设备句柄或宿主文件系统权限的 Robo 进程中执行。即使放进容器，容器也只降低主机风险；物理安全仍由动作 allowlist、租约、限位、deadline、控制器 watchdog、联锁和急停保证。

### 3.2 Robo 当前策略解释器的安全收益和边界

**当前实现事实。** `vibe/core/hardware/policy.py` 在执行前完整解析 AST，只接受有限语句和表达式；拒绝 import、属性访问、函数/类定义等动态能力。它只暴露纯函数、`observe`、manifest action、`verify`、`stop` 和 `print`，并限制源码 32 KiB、值/输出大小、执行语句、动作数、循环次数和总时长。每个动作仍构造 `HardwareCommand` 交给 `HardwareRuntime`；取消、超时、策略错误或硬件错误都会尝试停止设备；源码以 hash 和 URI 留档。

这些设计应作为真机策略执行的基线，但还不是完整安全证明：

- 目前只支持单设备，无法原子管理跨设备 lease、共享工作区互斥和传播停止；
- 超时后的软件 `stop()` 可能失败，且失败被吞掉后只返回原异常，不能替代独立 watchdog/E-stop；
- `PolicyRunStatus` 目前只有 `COMPLETED`，失败、取消、超时和 stop ACK 没有统一的持久化结果对象；
- 成功结果同时内嵌完整源码，32 KiB 源码、步骤输出和 16 KiB Trace payload 的边界需要用 artifact 引用协调；
- 策略执行前尚无面向用户的源代码、动作上限和风险 envelope 预览。

### 3.3 “代码成功执行”不等于“任务成功”

**官方事实。** CaP-X 的 `sandbox_rc == 0` 只说明 Python 没有执行错误；任务结论来自环境的 `reward` 与 `task_completed`。论文采用 Zero-Shot Pass@1，并在核心七个任务的不同配置上运行每个 task/tier 100 trials，而不是以一次无异常执行代表能力。[任务环境 `step`](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/tasks/base.py#L227-L298) [CaP-X 论文 Evaluation](https://arxiv.org/html/2603.22435v2#S4)

原始 CaP 的真实机器人实验说明方法可行，但论文也承认无法预先判断模型响应是否正确；其定量成功率主要来自受控仿真任务，不能外推为任意真机可靠性。[CaP 论文 Evaluation](https://arxiv.org/html/2209.07753#S4) [CaP 论文 Limitations](https://arxiv.org/html/2209.07753#S5)

**对 Robo 的设计推论。** 每个可发布策略都需要独立于代码自身的 task oracle：任务级后置条件、证据新鲜度、第三方/多视角观测、失败和 inconclusive 恢复规则。策略不能仅靠自己 `print("done")` 或读取自己的命令回执宣布成功。

## 四、CaP-X 的 agent、环境与轨迹协议

### 4.1 一次 trial 的状态模型

**官方事实。** CaP-Gym 是分层 Gymnasium 环境：低层 simulator/real robot 提供物理状态，高层 `CodeExecutionEnvBase` 是有状态代码执行器。高层 action 是 Python 字符串，源码定义为 `spaces.Text(max_length=4096)`；一个 turn 接收一段代码并执行至结束，代码内部可以多次调用 perception/control API。[任务环境初始化](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/tasks/base.py#L36-L151)

执行 namespace 会在同一 trial 的多个 turn 之间保留，只有环境 `reset` 才清空 globals。多轮 regeneration 也不会回滚模拟器或真机已经发生的物理变化。因此“重试”不是事务重放，后续代码必须基于当前观测继续，不能假设第一次动作没有发生。[代码执行上下文](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/tasks/base.py#L153-L205) [trial loop](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/trial.py)

这对 Robo 有直接约束：任何自动修复都必须先重新观察，并把命令区分为可安全重试、需要补偿和禁止自动重试三类；不能因为 HTTP 请求重发或模型输出 `REGENERATE` 就重复移动。

### 4.2 Agent HTTP 接缝

**官方事实。** CaP-X 已有外部 agent seam：向 `--server-url` 发送 OpenAI chat-completions 风格 POST。请求包括 `model`、`messages`、temperature/token 参数，可在 message 中携带 data-URL image；普通响应读取 `choices[0].message.content`。[官方 LLM client](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/llm/client.py#L180-L273)

官方客户端没有发送 trial header，也没有设置可依赖的 `user` 字段。Robo 自己的 `CapXEndpointAgent` 会发送 `x-capx-trial-id`，但方向相反：它是本地 smoke 调用 Bridge 的客户端行为，不能证明官方 runner 会提供该 header。

**当前实现事实。** `vibe/capx/app.py`、`harness_agent.py` 和 `models.py` 已实现兼容端点、文本/inline image 输入与响应转换；每次 HTTP completion 会创建一个新的 Robo harness session。CaP-X 的多轮状态仍由完整 messages 和未重置的物理环境承载，所以功能上可工作，但 Robo session 之间不会自然共享内部状态。

**对 Robo 的设计推论。** 正式 benchmark 需要 runner adapter 或 sidecar 显式分配并传递 `capx_trial_id`、task、tier、seed、worker、output directory 和 turn index；若不修改官方请求，则至少生成不可变 request hash，并在 runner 侧写一份 correlation manifest 做事后对齐。每个并发 worker 必须隔离 session、环境和 artifact 目录。

### 4.3 多轮反馈

**官方事实。** CaP-X 多轮配置会把前一段代码的 `stdout/stderr` 发回模型；视觉配置还可附当前 RGB、视觉差分模型（VDM）生成的前后差异文本或视频差异。agent 返回 `REGENERATE` 加 fenced Python，或返回 `FINISH`。源码常量 `MULTITURN_LIMIT = 10`，循环条件实际允许 `code_block_idx <= 10`，即索引 0—10 最多执行 11 个 code blocks；集成时应按固定提交的真实行为记录 turn，而不要只依赖常量名称。[trial 多轮实现](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/trial.py#L780-L864) [反馈 parser](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/utils/launch_utils.py#L310-L322)

论文的能力分层可以用来规划 Robo 的测试矩阵：

| 维度 | 官方含义 | Robo 应验证什么 |
| --- | --- | --- |
| S1 | 高层 primitive + privileged state | 先隔离验证代码组合与控制流 |
| S2 | 高层 primitive + 真实 RGB-D 感知 | 验证感知 API 和坐标/标定契约 |
| S3 | 较低层 primitive + 使用示例 | 验证 examples 能否弥补抽象降低 |
| S4 | 较低层 primitive，仅签名/docstring | 压测零样例 API grounding，不直接作为真机首发条件 |
| M1 | stdout/stderr 反馈 | 语法、参数和运行时错误修复 |
| M2 | 原始 RGB 反馈 | 从当前场景重新规划 |
| M3 | VDM 文本/视频差异反馈 | 从动作前后变化定位物理失败 |
| M4 | 低层 API + examples + VDM | 组合低层控制与多轮物理反馈 |

来源：[CaP-X 论文 Environment Configurations](https://arxiv.org/html/2603.22435v2#S3) [CaP-X 论文 Multi-Turn](https://arxiv.org/html/2603.22435v2#S3)

### 4.4 谁保存轨迹

**官方事实。** CaP-X runner 自己保存初始 prompt、raw/final code、每轮 response/reasoning、stdout/stderr、reward、`task_completed`、terminated/truncated、regeneration/finish/code-block 计数，以及配置开启时的图片和视频。外部 agent 端点只需要返回 completion，不需要把自己的工具轨迹塞回 HTTP 响应。[trial artifact 字段](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/trial.py#L72-L105) [runner 输出](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/runner.py#L122-L165)

**当前实现事实。** Robo Bridge 的 Trace 有自己的 `run_id`，且刻意只记录消息/图片数量、输出字符数等有界摘要，不复制全部 prompt/生成代码。这样能控制敏感内容和上下文大小，但单凭当前 Trace 无法完整复现 benchmark。

**对 Robo 的设计推论。** 两边各自保存权威 artifact，再用相关 ID 与哈希连接：CaP-X 保存 benchmark 代码、物理轨迹和评分；Robo 保存 provider/model/config、session/turn、审批、策略执行和硬件事件。不要在两个系统中无界复制图片、视频和 prompt。

CaP-X 用于 RL 的 parquet trajectory 是另一种产物：每行包含 `data_source`、prompt、`ability="agent"`、reward model ground truth 及 split/index/seed；reward runner 会重新按 seed reset 环境、抽取程序并单次执行。它不能替代在线 trial 的多轮物理轨迹，Robo 接训练流水线时应分别建模。[RL dataset exporter](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/cli/prepare_verl_dataset.py#L41-L83) [RL reward runner](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/verl_agent_reward/capx_franka_reward.py#L56-L130)

建议新增独立 correlation manifest：

```json
{
  "capx_commit": "53e9966d7a8e2fa7494676772bccc35280f5c0ed",
  "capx_trial_id": "...",
  "task": "...",
  "tier": "S1/M1",
  "seed": 0,
  "turn": 1,
  "request_sha256": "...",
  "capx_artifact_uri": "...",
  "robo_run_id": "...",
  "robo_session_id": "...",
  "policy_source_sha256": "...",
  "hardware_trace_uri": "..."
}
```

## 五、当前 Robo 实现映射

以下结论基于 2026-09-19 工作区快照；相关文件仍是当前分支上的未提交改动，不能把它们当作已合并发布能力。

| CaP/CaP-X 需要的能力 | 当前实现事实 | 判断 |
| --- | --- | --- |
| 能力发现与控制 API | `DeviceCapabilityManifest` 描述 actions/verifications；`HardwareRuntime.execute` 强制 manifest allowlist | 底座已具备；需补签名/docstring/examples、单位与坐标约束 |
| 代码内闭环 | `HardwarePolicyRunner` 支持 `if/for/while`、`observe`、action、`verify`、`stop`，并通过 `robo_run_policy` 接入模型与 UI | 安全收窄版产品链路已接通；尚缺 preview/不可变 artifact 审批 |
| 代码执行隔离 | 受限 AST 解释器，无 import/attribute/function definition | 适合真机方向；CaP-X 任意 `exec` 只留在隔离 benchmark |
| 权限与运动安全 | 连接、lease、arm、审批、Runtime 复验、失败停止 | 软件纵深防御已形成；仍缺独立 watchdog/E-stop 与实体硬件验收 |
| 感知 | snapshot + 有界 evidence；MuJoCo RGB 证据 | 原始证据链已有；策略可计算的 RGB-D/对象/位姿 API 缺失 |
| 任务验证 | `robo_verify`、evidence freshness、pending revision、`PolicyTaskOutcome` | 方向正确；真实任务 oracle 和恢复矩阵不足 |
| 多轮 benchmark agent | OpenAI-compatible Bridge 支持文本、图片和多轮 messages | 最小协议已接通；trial correlation 和 worker 隔离需加强 |
| 官方 benchmark 替代物 | `LocalCaPXBench` 只有小型折衣场景、有限 AST 调用和最多三轮 | 仅是 contract smoke；不能代表七任务、100 trials、视觉 tier 或官方评分 |
| 轨迹 | Runtime 有有界模型 Trace 与 JSONL；policy 有源码 hash/URI/steps | 缺 CaP-X trial/seed/task/artifact 的一等关联 |
| 技能复用 | 目前无可信 helper/skill 发布流水线 | 不能直接启用运行时层级函数生成 |
| 多设备 | Runtime/策略核心以单设备执行为主 | 无法覆盖相机—机械臂—传送带的协调黄金场景 |

关键源码：

- [`vibe/core/hardware/runtime.py`](../../vibe/core/hardware/runtime.py)：设备生命周期、安全仲裁、执行、观察、验证与停止；
- [`vibe/core/hardware/policy.py`](../../vibe/core/hardware/policy.py)：受限策略解释器；
- [`vibe/core/hardware/models.py`](../../vibe/core/hardware/models.py)：manifest、evidence、verification、policy 与 trace 模型；
- [`vibe/core/tools/builtins/robo.py`](../../vibe/core/tools/builtins/robo.py)：`robo_run_policy` 模型入口与兼容工具；
- [`vibe/capx/harness_agent.py`](../../vibe/capx/harness_agent.py)：CaP-X completion 到 Robo session 的转换；
- [`vibe/capx/local_bench.py`](../../vibe/capx/local_bench.py)：本地协议 smoke，不是官方 benchmark。

## 六、推荐目标架构

```text
生成/评测面（不可信）
  CaP prompt compiler -> 模型 -> 候选策略/helper
          |                         |
          +-> CaP-X 隔离 worker ----+-> 官方 reward/trajectory
                                    |
                                    v
发布面（可信构建）
  AST 检查 -> capability/type check -> 仿真/回放/故障注入
           -> 风险 envelope -> 人工批准 -> 不可变 policy/skill artifact
                                    |
                                    v
执行面（有界）
  HardwarePolicyRunner -> HardwareRuntime -> Adapter/控制器
       observe/act/verify        lease/limit/trace/stop
                                    |
                                    v
物理安全面（独立）
  controller watchdog + limiters + interlock + physical E-stop
```

### 6.1 策略发布与审批

新增一个显式的 policy lifecycle，而不是让模型拿到源码后直接运动：

1. **Generate**：由 manifest 自动生成 Hints，从版本化 examples/skills 生成候选代码；
2. **Compile**：完整 AST、名称、类型、单位、frame、参数 schema 和最坏情况循环/动作预算检查；
3. **Simulate**：在相同 manifest 版本的 simulator/replay 上执行，覆盖正常路径、抓空、传感器过期、断线和 stop 失败；
4. **Preview**：UI 展示源码、调用图、最大动作数、预计持续时间、设备/共享区域、速度/力/空间 envelope、验证条件和恢复动作；
5. **Approve**：用户一次批准这个不可变源码 hash 与 envelope，而不是批准未来任意代码；
6. **Execute**：解释器按步骤执行，Runtime 对每个动作重新校验当前 lease、armed 状态和物理限制；
7. **Verify/Recover**：由独立 oracle 判断 passed/failed/inconclusive；异常时记录 stop 请求与 ACK，禁止自动恢复 armed 状态。

审批只能减少重复交互，不能绕开 Runtime 的逐动作复验。源码、manifest 或 envelope 任一变化都必须重新编译和批准。

### 6.2 可信技能库，而不是在线任意 helper

CaP-X 论文展示了从成功 S3 rollouts 中抽取函数、筛选并验证后形成 skill library 的方向。[CaP-X 论文 Skill Library](https://arxiv.org/html/2603.22435v2)

Robo 可采用更严格的发布门：只从 `task_completed=true` 且证据完整的仿真/真机轨迹提取候选；正则或 AST 抽取后由模型归纳，但必须经过人工 review、契约测试、仿真、故障注入和版本签名。真机策略只能调用发布库中的 immutable skill ID。一次偶然成功或策略自己声明成功都不能自动提升为可信技能。

### 6.3 多设备策略

不要简单把当前 `device_id` 改成列表。应先定义：

- 策略开始前一次性获取所需设备和共享区域的 lease；获取失败时不移动任何设备；
- 所有 pose 明确 source/target frame 和 calibration revision；
- 相机、机械臂和传送带使用可比较的时间戳与 freshness；
- 共享空间由 Runtime 冲突组仲裁，而不是依赖策略中的布尔变量；
- 任一设备 fault/timeout 时执行预定义的组停止图，并分别记录每台设备 stop ACK；
- 跨设备动作若没有控制器级事务，就明确标为可补偿而非“原子”。

### 6.4 运行状态与事故包

将 `PolicyRunStatus` 扩展为至少 `running/completed/failed/cancelled/timed_out/stopping/stop_unconfirmed`，把异常也落为持久化 run result。事故包应引用：策略源码与 hash、manifest/skill/标定版本、审批 envelope、每步 observation/action/verification、设备 ACK、停止请求/确认、代码 Git commit、模型/provider 配置和外部 CaP-X artifact。

事故恢复时旧 lease 与 armed 状态必须失效；重新连接后先观察真实状态，人工确认后再武装。会话恢复不能等同于物理任务续跑。

## 七、分阶段落地方案与验收门

### 阶段 0：固定当前安全语义

- 为 `HardwarePolicyRunner` 增加失败/取消/超时/stop ACK 的持久化结果测试；
- 明确 `source_uri`、Trace payload 和 artifact 保留/脱敏规则；
- 把“动作完成”和“任务验证通过”作为不可回退的领域约束；
- 补无限循环、输出膨胀、过期证据、取消期间 stop 失败等测试。

通过门：所有非法 AST 在任何动作前拒绝；所有运行期失败都产生可审计终态；独立 simulator 故障注入可证明不会自动续跑。

### 阶段 1：只在模拟器开放策略产品链路

- 新增显式 `robo_policy_preview` / `robo_policy_execute` 边界，接入 App Server 和统一硬件 UI；
- preview 返回源码 hash、调用图、预算、设备和风险 envelope；execute 只接受已批准 artifact；
- 首版继续禁止动态函数定义与 import；
- 用固定毛巾折叠、抓取失败后重新观察、代码角度/弧度修复三个黄金场景验收。

通过门：用户能看到并批准将执行的确切策略；每个动作仍经过 Runtime；失败后设备进入可证明的安全状态或明确 `stop_unconfirmed`。

### 阶段 2：结构化感知与 API tier

- 扩展 manifest，生成签名、docstring、examples、单位、frame、前后置条件和幂等属性；
- 增加可信 RGB-D/对象/位姿/抓取点服务，策略只消费有界语义结果；
- 建立接近 CaP-X S1—S4、M1—M4 的内部矩阵，但不把 tier 名称当成安全等级。

通过门：同一任务分别在 privileged、真实感知、高层和低层 API 下得到可比较结果；每个验证都能追溯到原始 evidence 与标定版本。

### 阶段 3：CaP-X 正式评测与技能发布

- 固定 CaP-X commit、submodule、配置、模型、seed 和服务镜像；
- 用 runner adapter/sidecar 生成 correlation manifest，并隔离每个 worker；
- 先跑 S1 单 trial，再逐步进入视觉和多轮配置，最终按论文口径运行每 task/tier 100 trials；
- 从成功轨迹提取候选 helper，经验证后发布 skill artifact。

通过门：CaP-X 官方 artifact 与 Robo trace 可双向定位；结果报告同时显示代码执行率和 task success，不混用两者；技能无法由单次 rollout 自动晋升。

### 阶段 4：多设备与真机

- 实现坐标变换、时钟同步、跨设备 lease、共享区域互斥和组停止；
- 先 HIL/低速/限域，再接真实相机—机械臂—传送带；
- 上线独立控制器 watchdog、联锁与物理 E-stop，并演练通信中断和 stop 不确认。

通过门：断网、进程崩溃、传感器冻结、部分动作、设备 NACK 和 stop 失败均有已验证响应；任何软件路径都不能绕过硬件限位与急停。

## 八、不应采用的做法

- 不把原始 CaP `exec_safe` 或 CaP-X `SimpleExecutor` 当作真机 sandbox；
- 不在 Robo 主进程安装并执行任意生成代码，也不把容器隔离误当作物理安全；
- 不把 LLM 生成的高频关节/力控制循环下发给设备；
- 不在 regeneration 时自动重放前一段物理动作；
- 不用 `sandbox_rc == 0`、命令 ACK、stdout 或策略自报完成替代任务验证；
- 不让未审查的成功轨迹自动成为长期技能；
- 不把图片 URI 暴露给策略后允许其任意读取文件或网络；
- 不让 session 恢复、模型重试或 `--yolo` 恢复 lease/armed 权限；
- 不以本地 `LocalCaPXBench` smoke 成功宣称通过官方 CaP-X benchmark。

## 九、一手来源索引

### Code as Policies

- [官方项目页](https://code-as-policies.github.io/)
- [论文：Code as Policies: Language Model Programs for Embodied Control](https://arxiv.org/html/2209.07753)
- [Google Research 官方源码目录](https://github.com/google-research/google-research/tree/master/code_as_policies)
- [Interactive Demo（固定提交）](https://github.com/google-research/google-research/blob/4700efb9afa54286b0e04473ba80a13e8461e25f/code_as_policies/Interactive_Demo.ipynb)
- [LMP Examples 与层级生成示例（固定提交）](https://github.com/google-research/google-research/blob/4700efb9afa54286b0e04473ba80a13e8461e25f/code_as_policies/LMP%20Examples.ipynb)
- [Reactive Controllers 示例（固定提交）](https://github.com/google-research/google-research/blob/4700efb9afa54286b0e04473ba80a13e8461e25f/code_as_policies/Experiment_%20Reactive%20Controllers%20on%20Toy%20Tasks.ipynb)

### CaP-X

- [官方仓库](https://github.com/capgym/cap-x)
- [固定研究基线提交](https://github.com/capgym/cap-x/commit/53e9966d7a8e2fa7494676772bccc35280f5c0ed)
- [论文 v2：CaP-X](https://arxiv.org/abs/2603.22435v2)
- [高层代码环境与执行器](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/tasks/base.py)
- [trial、多轮反馈与 artifact](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/trial.py)
- [OpenAI-compatible LLM client](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/llm/client.py)
- [launch utilities](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/utils/launch_utils.py)
- [Adding APIs](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/adding-apis.md)
- [Adding Environments](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/adding-environments.md)

### Robo 当前实现

- [`HardwareRuntime`](../../vibe/core/hardware/runtime.py)
- [`HardwarePolicyRunner`](../../vibe/core/hardware/policy.py)
- [硬件领域模型](../../vibe/core/hardware/models.py)
- [`robo_*` 工具](../../vibe/core/tools/builtins/robo.py)
- [CaP-X Bridge](../../vibe/capx/app.py)
- [CaP-X Harness Agent](../../vibe/capx/harness_agent.py)
- [本地 CaP-X 协议 smoke](../../vibe/capx/local_bench.py)
