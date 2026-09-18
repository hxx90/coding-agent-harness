# Robo 硬件 Harness 改造方案

状态：本地 MVP 已实现；实体硬件与生产级安全能力待后续阶段

本文记录对现有源码的扫描结果，以及将其改造成 Robo 硬件智能体 Harness 的推荐方案。硬件边界和安全约束已经确定，首个可运行切片已经实现并通过本地测试。

## 当前实现状态（2026-09-17）

已实现：

- `vibe/core/hardware` 的设备 Manifest、Adapter Port、租约、显式武装/解除武装、执行、观察、停止和 JSONL Trace；
- Mac 可运行的确定性机械臂 Adapter；
- 受 Runtime 仲裁的 `robo_devices`、`robo_observe`、`robo_execute`、`robo_sequence`、`robo_verify`、`robo_stop`；
- MuJoCo 观察可生成会话内 RGB 证据帧，并将图片注入支持视觉的模型上下文；
- 命令完成与任务完成已经拆分，任务结论只能来自显式后置条件验证；
- stdio MCP 厂商 Adapter，且要求隐藏原始设备 Tool，防止模型绕过 Runtime；
- CaP-X OpenAI-compatible Agent Bridge，支持文本、data URL 图片和多轮反馈；
- 不执行任意 Python 的本地 CaP-X 风格“错误—观察—修复”烟雾测试。
- 最多 200 条的模型可见 Trace，单个事件载荷上限 16 KiB；完整 JSONL 仍保存在模型上下文外。

仍未完成：实体设备 Adapter、物理看门狗/急停、远程 HTTP 设备网关、完整 record/replay、专用硬件 UI，以及只能在 Linux/NVIDIA 上验收的官方 CaP-X 全量 Benchmark。运行方式和当前 Mac 探测结果见 [本地运行手册](robo-harness-local-runbook.md)。

## 一、核心决策

保留 Vibe 成熟的交互、模型调用和会话编排能力，但不能把硬件简单地实现为几条 Shell 或串口工具，也不能把设备逻辑散落在 AgentLoop 和 UI 中。

应新增一个有深度的 `HardwareRuntime` 模块，由服务端持有。这个模块通过少量稳定接口隐藏以下复杂性：

- 设备发现与能力协商；
- 连接和断线重连；
- 指令排队、互斥和执行确认；
- 设备租约与控制权；
- 遥测、看门狗和时间同步；
- 安全策略、急停和故障恢复；
- 真实硬件、模拟器及回放环境之间的差异。

第一条完整链路应同时连接模拟器和一台真实设备。MCP 可以作为最快的原型接入层，但稳定后的正式产品路径应进入原生 `HardwareRuntime`，由它掌握安全和执行权。

## 二、源码扫描结果

当前项目使用 Python 3.12，终端 UI 基于 Textual。

```text
Textual CLI / ACP / 未来客户端
                │
       序列化 App Server API
                │
         SessionBackend 接口
            ┌───┴───┐
   旧 AgentLoop   Unified Harness 适配器
            └───┬───┘
       类型化工具、回调、Effect
                │
     文件 / Shell / MCP / Connector
```

可以直接复用的模块：

- `vibe/core/agent_loop`：事件驱动的模型与工具循环，支持中断和流式输出；
- `vibe/core/tools`：Pydantic 参数、结果、配置、状态、权限和 UI 展示；
- `vibe/app_server`：会话、Turn、回调、Effect、资源和序列化事件的统一边界；
- `vibe/core/session`：本地持久化、进程租约、恢复、分叉、回退和检查点；
- MCP、Connector、Plugin、Hook、Skill、多模型 Provider 和 ACP。

扫描中发现的关键约束：

- 一次模型响应中的多个工具调用可能并发执行，因此硬件指令必须在工具层之下按设备和冲突组串行化；
- 取消 Python 工具任务不代表电机已经停止，取消流程必须发送停止指令并等待硬件确认；
- 现有权限只支持命令、文件、目录和 URL，无法表达设备、执行器、运动区域、速度、力矩或能量风险；
- `PublicSessionState` 只是可渲染投影，不是运行时状态存储，因此实时设备状态应成为 App Server 管理的独立资源；
- 当前会话恢复只重建对话状态，不能据此恢复任何真实设备的运动状态；
- 通用工具已经可以通过通用 Effect 展示，不应为每个设备增加 App Server 分支；只有需要专门硬件卡片时才新增一个统一的硬件展示类型。

当前代码中没有串口、CAN、ROS、GPIO、硬件发现、硬件看门狗、急停、指令确认和 HIL 测试能力。

## 三、模块边界设计

第一阶段在 `vibe/core/hardware/` 下实现硬件模块。暂时保留内部 `vibe` Python 包名，先将用户可见品牌和命令改为 Robo，避免在产品行为尚未稳定时进行大规模、低收益的 import 重命名。

```text
Robo 工具 + App Server 硬件资源
                 │
         HardwareRuntime 接口
                 │
   ┌─────────────┼─────────────┐
   │ 设备注册表  │ 安全监督器  │ 执行与观测器 │
   └─────────────┼─────────────┘
                 │
          DeviceAdapter 接缝
       ┌─────────┼──────────┐
     模拟器    录制回放    真实设备驱动
                           │
                    串口 / CAN / ROS 2 / 网络
```

### `HardwareRuntime` 公共接口

接口保持小而稳定，并使用硬件领域概念：

```python
class HardwareRuntime(Protocol):
    async def discover(self) -> DeviceCatalog: ...
    async def connect(self, ref: DeviceRef) -> DeviceSnapshot: ...
    async def acquire(self, request: LeaseRequest) -> DeviceLease: ...
    async def execute(self, command: CommandEnvelope) -> CommandReceipt: ...
    async def observe(self, request: ObservationRequest) -> ObservationStream: ...
    async def stop(self, request: StopRequest) -> StopReceipt: ...
    async def release(self, lease: DeviceLease) -> None: ...
    async def close(self) -> None: ...
```

这个接口不应出现串口号、ROS Topic、CAN Frame 或厂商 SDK 对象；这些细节属于具体 `DeviceAdapter`。

### 核心领域模型

- `DeviceRef`：稳定设备身份、适配器类型、显示名称和连接提示；
- `CapabilityDescriptor`：指令与观测 Schema、单位、限制、互斥组和安全等级；
- `CommandEnvelope`：指令 ID、会话 ID、设备 ID、租约 ID、序列号、截止时间、能力名称、类型化参数和演练模式；
- `CommandReceipt`：接受或拒绝、ACK、开始与结束时间、最终状态、测量值、警告和失败信息；
- `Observation`：设备时间、主机时间、序列号、质量、单位、类型化数据和来源；
- `DeviceSnapshot`：连接、安全状态、当前租约、能力、健康度、活动指令和有限数量的最新观测；
- `HardwareFailure`：稳定错误类别、是否可重试、操作员动作、技术细节和设备是否确认安全。

### Adapter

从第一天就提供三个 Adapter，并让它们通过同一套契约测试：

1. `SimulatorAdapter`：用于确定性开发和 CI；
2. `RecordReplayAdapter`：无需实体硬件即可复现现场问题；
3. `PhysicalDeviceAdapter`：连接第一种真实机器人或控制器。

串口、CAN、ROS 2、BLE 或厂商 SDK 应位于真实设备 Adapter 内部，不得泄漏到模型工具接口。

## 四、硬件 Harness 必须补充的能力

### 设备生命周期

- 设备发现、稳定身份、连接、断开、重连和健康检查；
- 固件与协议版本协商、能力发现；
- 配置校验，密钥配置只暴露环境变量名；
- 热插拔检测，且不能阻塞 CLI 启动。

### 指令正确性

- 唯一指令 ID、单调递增设备序列号以及 ACK/NACK；
- 明确的截止时间和最大执行时长；
- 每设备指令队列和能力冲突组；
- 幂等键，只有已证明安全的操作才允许自动重试；
- 设备支持时使用 prepare/commit/abort 事务式指令序列；
- 所有运动和传感器数据必须携带单位与坐标系。

### 安全能力

- `disconnected -> safe -> armed -> executing -> fault -> estopped` 状态机；
- 不依赖 LLM 或当前 Turn 的带外急停；
- 在模型和工具层之下强制执行速度、加速度、力/电流、温度、位置、工作空间和时长限制；
- 由运行时或独立安全控制器持有的心跳和失联看门狗；
- 使用租约保证同一设备或互斥能力只有一个控制者；
- 必须显式上电解锁和解除武装，`--yolo` 不得绕过危险硬件策略；
- 取消任务时发送停止指令、等待停止确认，并返回 `safe`、`unknown` 或 `unsafe`；
- 危险机器必须有独立物理安全控制器或联锁，软件策略只能作为纵深防御。

### 可观测性和回放

- 高频遥测存储在 LLM 上下文之外；
- 只向模型和 UI 提供有界、降采样后的观测；
- 记录设备时间与主机时间，并提供时钟同步；
- 建立“提示词 -> 工具调用 -> 审批 -> 指令 -> ACK -> 遥测”的完整 Trace；
- 支持确定性录制回放和故障注入；
- 统计指令延迟、心跳丢失、重连、故障、急停、审批和安全策略拒绝。

### 故障分类

至少区分：参数校验失败、权限拒绝、安全策略拒绝、租约冲突、设备断开、协议不兼容、发送前超时、发送后超时、设备 NACK、部分执行、传感器过期、看门狗触发、急停和物理状态未知。

“物理状态未知”必须经过人工检查或明确的恢复流程，不能自动重新武装。

### 持久化和会话恢复

持久化指令意图、审批、执行回执和有限的观测摘要，但不能把连接句柄、活动租约或 `armed` 状态作为可恢复权限。进程重启或恢复会话时必须：

1. 失效旧租约；
2. 如果通信仍可用，发送安全停止；
3. 以只读或安全模式重新连接；
4. 核对真实设备状态；
5. 要求用户显式重新武装后才能运动。

## 五、与现有 Vibe 架构的集成

### 提供给模型的工具

首版只保留一组小而明确的产品工具：

- `robo_devices`：发现设备并读取能力；
- `robo_observe`：读取观测或等待有界条件；
- `robo_verify`：根据设备声明的后置条件验证任务结果并引用观察证据；
- `robo_execute`：校验并执行一条类型化能力指令；
- `robo_sequence`：执行有界且已校验的多步计划；
- `robo_stop`：停止单台或全部设备，并始终可用。

所有工具只调用 `HardwareRuntime`，绝不直接打开硬件端口。Adapter 可以提供能力 Schema，但最终安全校验和执行权必须留在 Runtime。

### App Server 扩展

App Server 应持有规范化的 `HardwareResource`，提供设备列表/读取、连接/断开、武装/解除、租约、停止和诊断方法。增加设备快照、故障和遥测摘要通知，使 Textual、ACP 和未来 Web/移动客户端共享同一状态。

新增一个统一的 `hardware` Effect 展示类型，用于渲染硬件指令卡片。未知的第三方能力继续走通用 Effect。只有当 UI 需要明确显示危险等级、目标设备、限制、运动预览、超时和停止行为时，才新增专用硬件审批结构。

### UI 能力

- 常驻设备与安全状态栏；
- 醒目的全局急停快捷键和命令；
- 设备选择、连接和武装控制；
- 实时但限频的遥测面板；
- 显示目标、单位、限制、ACK 和最终状态的指令卡片；
- 故障时间线和引导式恢复操作；
- 明确区分模拟器、回放和真实硬件。

## 六、Robo 品牌迁移策略

品牌迁移应保持兼容，不能做全仓库字符串替换。

第一阶段包括：

- 提供 `robo`、`robo-acp` 和 `robo-app-server` 命令；
- 将名称、横幅、欢迎页、帮助、系统身份和文档改为 Robo；
- 后续引入 `ROBO_HOME` / `~/.robo`，并提供从现有 Vibe 配置的一次性导入；
- 替换或禁用 Mistral 专属的登录、升级、反馈、遥测、Vibe Code 和发布地址；
- 保留 Apache-2.0 许可证和上游版权声明；
- 暂时保留 `vibe*` 命令与配置别名；
- 在硬件功能稳定前保留 `vibe.*` Python import，降低迁移风险。

当前 Git `origin` 仍指向 `mistralai/mistral-vibe`。推送 Robo 修改前，应创建 Robo 自己的仓库并设为 `origin`，原 Mistral 仓库改名为 `upstream`，禁止把 Robo 修改推送到上游仓库。

## 七、交付阶段

### 阶段 0：Fork 和产品外壳

完成用户可见品牌、命令和配置目录迁移，建立 Robo 自有远程仓库，删除或禁用 Mistral 云服务专属功能，并保持现有测试通过。

### 阶段 1：模拟器优先的完整链路

通过 MCP 实现模拟器和一个真实 Adapter，跑通：发现 -> 连接 -> 观测 -> 审批 -> 指令 -> ACK -> 停止。根据真实通信记录定义协议，避免一开始设计过度抽象的硬件模型。

### 阶段 2：原生 HardwareRuntime

将稳定的领域契约迁入 `vibe/core/hardware`，增加 App Server 硬件资源、原生 Robo 工具、硬件 Effect、会话安全语义和 UI。MCP 继续作为扩展接缝，但不再承担安全控制权。

### 阶段 3：可靠性和安全

增加看门狗、租约过期、故障恢复、录制回放、模拟器故障注入、HIL 测试、长时间稳定性测试及安全证据。

### 阶段 4：多设备和远程设备

增加多设备调度、远程网关、设备身份认证、加密通信、设备组策略、固件升级流程和基于角色的操作权限。

## 八、保留、替换和延后

保留：

- Provider 抽象、AgentLoop/Unified Harness 接缝、App Server、类型化工具、回调、会话历史、Skill、Hook、MCP、Plugin、Textual 和 ACP；
- 有界的模型可见结果，以及由服务端掌握的规范状态。

尽早替换或禁用：

- Mistral 登录与欢迎流程、升级服务、产品遥测端点、反馈、Vibe Code/Teleport 和发布配置。

首版延后：

- 语音、云端设备组管理、任意第三方驱动、由多个 Agent 自主控制真实硬件，以及完整 Web UI。

Subagent 可以分析、模拟和诊断，但所有物理指令必须经过唯一的 Runtime 安全仲裁器。

## 九、MVP 验收标准

- 同一场景无需修改工具即可在模拟器和真实 Adapter 上运行；
- 可以发现、检查、连接设备，并必须显式武装；
- 每条物理指令都有指令 ID、截止时间、审批记录、ACK 和最终回执；
- 冲突的并发工具调用会被确定性地排队或拒绝；
- 中断和急停最终返回已确认的 `safe` 或明确的 `unknown` 状态；
- 断线、遥测过期、超时、NACK 和看门狗触发均有自动化测试；
- 进程重启和会话恢复后，真实硬件始终处于未武装状态；
- 原始遥测可以回放，同时 LLM 上下文保持有界；
- 具备单元测试、Adapter 契约测试、模拟器测试和 HIL 测试。

## 十、第一个实现切片

若首台硬件尚未确定，默认使用确定性的差速机器人模拟器和串口 JSON Adapter，只定义五个指令：`get_state`、`arm`、`set_velocity`、`stop`、`disarm`。

这个范围足以验证设备生命周期、安全、权限、执行、遥测和恢复的完整闭环，同时不会过早固化所有机器人能力。
