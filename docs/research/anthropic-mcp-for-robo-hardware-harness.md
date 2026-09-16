# MCP 用于 Robo 多型号机械臂接入的协议研究

日期：2026-09-17
结论依据：Anthropic 官方公告、MCP `2026-07-28` 稳定规范、MCP 官方文档与官方工具。本文只引用一手资料。

## 结论先行

Anthropic 最初将 MCP 定义为连接 AI 应用与数据源、工具的开放标准，而不是机器人实时控制协议。当前规范适合让 Robo 发现不同厂商适配器、读取能力和状态、调用有界的高层动作，以及统一调试这些接入；它不应承担伺服环、轨迹插补、硬实时通信、急停或最终安全仲裁。[Anthropic 官方公告](https://www.anthropic.com/news/model-context-protocol) [MCP 架构规范](https://modelcontextprotocol.io/specification/2026-07-28/architecture)

推荐边界是：

```text
用户 / LLM
    │
Robo Host（上下文、审批、权限、界面、追踪）
    │ 每个 Server 对应一个 MCP Client
    ├── MCP Client ── 厂商 A Adapter Server ──┐
    ├── MCP Client ── 厂商 B Adapter Server ──┼── HardwareRuntime / 安全仲裁器
    └── MCP Client ── 模拟器 Adapter Server ──┘        │
                                                  厂商 SDK / ROS 2 / CAN / 串口
```

其中 MCP 是“可插拔接入与语义交换层”；`HardwareRuntime` 是“有状态的执行与安全层”。即便某个 Adapter Server 直接嵌入 Runtime，这两个职责也要在代码和权限上分开。

## 版本基线：不要照搬旧版 MCP 示例

本文以当前稳定版 `2026-07-28` 为准。该版是无状态协议：每次请求都必须携带协议版本和客户端能力，服务端不能从同一连接上的历史请求推断上下文；需要跨请求的状态必须用显式句柄表示。[基础协议：Statelessness](https://modelcontextprotocol.io/specification/2026-07-28/basic#statelessness)

这和 `2025-11-25` 及更早版本有明显差别：当前 Streamable HTTP 已移除 GET 流端点和协议级 Session；现代协议使用逐请求元数据与 `server/discover`，旧版才使用 `initialize` 握手。[Streamable HTTP 版本变化](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http) [版本兼容规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)

对 Robo 的含义（设计推论）：

- 不把 MCP 连接或子进程存活视为“设备仍被当前用户控制”。`lease_id`、`device_id`、`run_id`、幂等键和截止时间必须显式出现在领域请求中。
- 不用旧版 `initialize` 结果作为永久能力缓存；使用当前 `server/discover`，并遵守缓存 TTL 与能力变化通知。
- 若要兼容已有旧 MCP 机械臂 Server，应单独实现协议时代协商，不应在业务代码里混用两套生命周期语义。

## Host、Client、Server 应如何对应 Robo

### 官方协议事实

- Host 是容器与协调者：创建和管理多个 Client，控制连接权限和生命周期，执行安全政策与用户授权，协调 LLM，并聚合上下文。[架构：Host](https://modelcontextprotocol.io/specification/2026-07-28/architecture#host)
- 一个 Client 只与一个 Server 通信；Host 可以拥有多个 Client，并由 Host 保持 Server 之间的安全边界。[架构：Clients](https://modelcontextprotocol.io/specification/2026-07-28/architecture#clients)
- Server 聚焦提供特定能力，通过 MCP 暴露 tools、resources、prompts；Server 不应看到完整对话，也不应看到其他 Server 的内容，跨 Server 协调由 Host 控制。[架构：Servers 与设计原则](https://modelcontextprotocol.io/specification/2026-07-28/architecture#servers)
- `serverInfo` 是 Server 自报的信息，只适合显示、日志和兼容性诊断，不能用来做安全决策。[Discovery：serverInfo](https://modelcontextprotocol.io/specification/2026-07-28/server/discover#data-types)

### Robo 映射（设计推论）

- **Robo CLI / App Server 是 Host**：持有模型会话、用户审批、设备授权政策和跨设备任务计划。
- **每个接入实例对应一个 MCP Client**：例如 UR5e 实验台、Franka 模拟器、远程 xArm 网关分别建 Client，避免共享隐式状态。
- **每种厂商或协议族实现 Adapter Server**：在 Server 内转换统一 Robo 领域指令与厂商 SDK、ROS 2 Action、CAN 或串口协议。
- **Server 名称不能作为设备身份**：Robo 注册表应保存显式的 Adapter 安装 ID、设备序列号、配置签名和信任状态。
- **MCP Server 不能互相直接拼装动作**：多机械臂任务的资源冲突、同步和租约由 Host 下方的统一 Runtime 仲裁。

## 三种 Server Primitive 的正确分工

| MCP Primitive | 官方交互模型 | Robo 中适合承载 | 不适合承载 |
| --- | --- | --- | --- |
| Tools | 模型可发现并调用；协议建议始终让人能够拒绝调用 | 发现设备、拍摄有界快照、诊断、规划、高层有界动作、软停止请求 | 1 kHz 伺服命令、急停、安全联锁 |
| Resources | 由应用决定何时纳入上下文，可列出、读取、订阅变化 | 设备清单、能力 Manifest、状态快照、标定、故障、运行 Trace、低频遥测摘要 | 无界视频流、高频关节状态、可靠事件总线 |
| Prompts | 用户显式选择的模板 | “校准机械臂”“诊断抓取失败”“生成折衣计划”等操作员工作流 | 自动注入且直接触发运动的隐藏指令 |

依据：[Tools 用户交互模型](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#user-interaction-model) [Resources 用户交互模型](https://modelcontextprotocol.io/specification/2026-07-28/server/resources#user-interaction-model) [Prompts 用户交互模型](https://modelcontextprotocol.io/specification/2026-07-28/server/prompts#user-interaction-model)

### Tools

Tool 使用 JSON Schema 描述输入，可以提供 `outputSchema` 和 `structuredContent`；这适合把不同厂商返回值归一为可验证的设备快照、计划回执和执行回执。[Tools 数据类型与结构化输出](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#data-types)

Robo 的首批跨型号稳定工具可收敛为：

- `robo.devices.list`：列出 Runtime 已识别的设备；
- `robo.device.describe`：返回标准化能力、单位、坐标系和限制；
- `robo.observe`：获取有界的场景或设备快照；
- `robo.plan`：生成不产生物理副作用的候选计划；
- `robo.execute`：提交经过约束验证的高层动作或计划；
- `robo.stop`：请求受控停止，但不能冒充独立硬件急停。

机械臂型号差异不应该体现为给模型暴露几百个厂商原生命令。`tools/list` 可以动态变化，多个 Server 也可能出现同名 Tool；官方要求聚合 Client 自行消歧，且不能依赖并不唯一的 Server 名称。[Tools：命名冲突](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool-names)

当设备很多时，可按官方 Client 最佳实践做渐进式发现：Host 先保留轻量目录，仅在任务需要时载入完整 Tool Schema，并在 `tools/list_changed` 后刷新索引。[Client 最佳实践：Progressive Tool Discovery](https://modelcontextprotocol.io/docs/2026-07-28/develop/clients/client-best-practices#progressive-tool-discovery)

Tool 的 `readOnlyHint`、`destructiveHint`、`idempotentHint` 和 `openWorldHint` 只是提示，规范明确要求来自不可信 Server 的注解不能作为调用决策依据。[ToolAnnotations 规范](https://modelcontextprotocol.io/specification/2026-07-28/schema#toolannotations)

对 Robo 的含义（设计推论）：`destructiveHint: false` 不能替代风险分析，`idempotentHint: true` 不能自动允许重试机械臂动作。重试、速度、力矩、工作空间和碰撞限制必须由 Runtime 的可信配置决定。

### Resources

Resources 以 URI 标识，支持 `resources/list`、`resources/read`、URI Template 和特定资源更新订阅；收到 `notifications/resources/updated` 后，Client 再读取最新内容。[Resources 规范](https://modelcontextprotocol.io/specification/2026-07-28/server/resources)

推荐的 Robo 资源 URI（设计推论）：

```text
robo://devices/{device_id}/manifest
robo://devices/{device_id}/snapshot
robo://devices/{device_id}/calibration
robo://devices/{device_id}/faults/latest
robo://runs/{run_id}/trace
```

能力 Manifest 至少应表达关节、末端执行器、传感器、支持的高层动作、单位、坐标系、标定版本、工作空间和厂商固件信息。这里的“设备能力”是 Robo 领域模型，不等于 MCP 的协议 Capability。

官方说明变更通知是 best effort，跨重连不保证每条都发送或收到，Client 应配合轮询保证新鲜度。[架构指南：Notification Best Effort](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture#real-time-updates-notifications)

对 Robo 的含义（设计推论）：Resource 订阅可驱动 UI 状态卡和低频摘要，不能作为唯一的传感器、安全状态或控制反馈来源；重连后必须重新订阅并读取完整快照。

### Prompts

Prompts 是由 Server 定义、由用户显式选择的结构化消息模板，Server 必须声明 `prompts` capability，Client 通过 `prompts/list` 和 `prompts/get` 使用。[Prompts 规范](https://modelcontextprotocol.io/specification/2026-07-28/server/prompts)

对 Robo 的含义（设计推论）：Prompt 适合包装厂商推荐的校准、诊断、维护流程，但 Prompt 内容仍是不可信输入，必须经过 Host 的指令隔离和 Tool 权限政策，不能获得额外硬件权限。

## Capability Negotiation：能协商什么，不能协商什么

### 官方协议事实

- 当前协议每次请求在 `_meta.io.modelcontextprotocol/protocolVersion` 中声明版本，并在 `_meta.io.modelcontextprotocol/clientCapabilities` 中声明 Client 能力。[基础协议 `_meta`](https://modelcontextprotocol.io/specification/2026-07-28/basic#meta)
- 每个 Server 都必须实现 `server/discover`，返回支持的协议版本、Server 能力和自报身份；Client 调用 discovery 本身是可选的，但适合在首次使用前探测。[Discovery 规范](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
- Server 的 `tools`、`resources`、`prompts`、`logging` 等 capability 表示它实现了哪些 MCP 功能；Client 的 `elicitation` 等 capability 表示它能处理哪些 Server 输入请求。双方必须遵守已声明能力。[架构：Capability Negotiation](https://modelcontextprotocol.io/specification/2026-07-28/architecture#capability-negotiation)

### Robo 需要再增加一层领域协商（设计推论）

MCP capability 不描述“这台机械臂有六个关节、是否有夹爪、最大载荷、相机、控制频率或安全模式”。Robo 必须在 MCP 之上定义版本化的 `DeviceCapabilityManifest`，通过 Resource 或 `device.describe` Tool 交换，并独立做兼容性检查。

建议启动顺序：

1. `server/discover`：确认 MCP 版本和 primitives；
2. `tools/list` / `resources/list`：确认 Server 暴露面；
3. 读取设备 Manifest：确认型号、固件、能力 Schema、单位和坐标系；
4. 只读连接与健康检查；
5. Runtime 验证安全配置；
6. 用户显式授予本次设备租约并武装；
7. 才允许执行运动类 Tool。

## Transport 选择

### stdio

stdio 模式由 Client 启动 MCP Server 子进程，通过 stdin/stdout 传输逐行 JSON-RPC；Server 的 stdout 不能混入普通文本，日志应写 stderr。进程意外退出时 Client 应重启，但进行中的请求会丢失，订阅也必须重建。[stdio 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)

适合 Robo 的场景（设计推论）：在开发电脑上启动一个厂商 SDK Adapter、模拟器 Adapter 或本地 ROS Bridge。优点是部署简单、访问面小；缺点是 Server 继承本地权限，因此必须限制可执行命令、环境变量、文件和网络权限。

官方安全指南建议本地 Server 优先用 stdio 限制访问到当前 Client；安装本地 Server 前必须展示完整启动命令并取得明确同意，并建议以最小权限沙箱运行。[本地 MCP Server 安全](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices#local-mcp-server-compromise)

### Streamable HTTP

当前 Streamable HTTP 使用单一 POST 端点。每个 JSON-RPC 请求是独立 POST；响应可以是单个 JSON，也可以是仅属于该请求的 SSE 流。长期变更通知通过 `subscriptions/listen` 的响应流发送。[Streamable HTTP 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)

Server 必须校验 `Origin` 防止 DNS rebinding，本地部署应只绑定 `127.0.0.1`，并应为所有连接实现认证。[Streamable HTTP：Security & Endpoint](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#security-endpoint)

适合 Robo 的场景（设计推论）：远程机器人网关、实验室设备服务或隔离网络中的仿真集群。MCP HTTP 端点不应直接暴露到机械臂控制总线；网关后方仍要有 Runtime、网络分区、设备身份和独立看门狗。

### MCP 不替代底层设备协议

这是设计推论，而非规范原文：stdio 和 Streamable HTTP 只定义 Host 与 MCP Server 的消息传输。串口、CAN、EtherCAT、ROS 2、厂商实时接口及相机媒体流都应留在 Adapter Server 后方，MCP 不应把这些协议细节泄漏给 LLM。

## Sampling、Elicitation、Progress、Logging 如何使用

### Sampling：新实现不要采用

Sampling 允许 Server 经 Client 请求一次 LLM 生成，Client 保持模型选择和权限控制；规范也要求应让人能够拒绝请求。[Sampling 规范](https://modelcontextprotocol.io/specification/2026-07-28/client/sampling)

但 Sampling 已在 `2026-07-28` 被弃用，新实现不应采用，迁移路径是 Server 直接集成模型 Provider API。[Deprecated Features](https://modelcontextprotocol.io/specification/2026-07-28/deprecated)

对 Robo 的结论：不要让机械臂 Adapter 通过 Sampling 再启动一个隐藏 Agent。任务推理留在 Robo Host；Adapter 只完成确定性转换、验证、执行和观测。

### Elicitation：用于补充输入，不是安全控制器

Elicitation 允许 Server 在一次调用中请求用户补充信息。Form 模式收集结构化非敏感数据；URL 模式用于不能经过 MCP Client 的密钥、Token 或付款等敏感交互。Client 必须标明请求来自哪个 Server，并允许用户 accept、decline 或 cancel。[Elicitation 规范](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)

适合 Robo 的场景（设计推论）：要求用户选择工具头、确认夹具编号、提供衣物材质，或进入厂商网页完成设备授权。不适合承担急停确认、毫秒级碰撞响应或唯一的危险动作审批；这些必须由 Robo Host 与 Runtime 强制执行。

### Progress：用于人类进度，不是遥测

Server 可以为长操作发送单调递增的 progress；但规范明确允许 Server 完全不发、按任意频率发送，且建议限频。[Progress 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/progress)

适合显示“标定第 3/8 步”或“规划完成 70%”。不适合传输关节角、碰撞距离或心跳，也不能用 progress 到达与否判断机械臂安全。

### Tasks 扩展：可表示长任务，但取消仍不是停止保证

Tasks 已从 Core 独立为需要显式协商的官方扩展。它可返回持久 `taskId`，让 Client 在重连后继续轮询、处理中途输入并取得最终结果；适合折衣、批量标定等长流程。官方同时明确 `tasks/cancel` 是 cooperative，Server 只确认取消意图，并没有停止工作的义务。[Tasks 扩展](https://modelcontextprotocol.io/extensions/tasks/overview)

对 Robo 的结论（设计推论）：Task 只记录工作流状态，不能代表设备租约或真实运动状态；`cancelled` 也不能替代设备停止 ACK。

### Logging：当前已弃用

MCP Logging 已在 `2026-07-28` 弃用。官方迁移建议是 stdio Server 写 stderr，结构化可观测性使用 OpenTelemetry。[Logging 规范与弃用说明](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/logging)

对 Robo 的结论：不要新建依赖 `notifications/message` 的观测架构。统一用 OpenTelemetry Trace/Metric/Log，把 `session_id`、`tool_call_id`、`device_id`、`lease_id`、`command_id`、`run_id` 串起来；stdio 的本地开发日志走 stderr。当前规范还为 OpenTelemetry W3C Trace Context 保留了 `_meta.traceparent`、`tracestate` 和 `baggage` 字段。[基础协议 `_meta`](https://modelcontextprotocol.io/specification/2026-07-28/basic#meta)

## Cancellation 为什么绝不能当急停

官方规范明确说明：取消是可选的，Server 在请求无法取消时可以忽略；取消还存在网络延迟和“处理已经完成”的竞态。stdio 通过 `notifications/cancelled` 取消，Streamable HTTP 通过关闭该请求的 SSE 响应流取消。[Cancellation 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/cancellation)

因此以下结论属于有直接协议证据支持的设计推论：

- 取消 `tools/call` 只表示“Host 不再等待或请求 Server 停止工作”，不证明电机已停止。
- `robo_stop` 只能返回带设备 ACK 的停止回执；未收到 ACK 时状态必须是 `unknown` 或 `unsafe`，不能显示成功。
- 急停必须走不依赖当前 MCP 请求、网络流、LLM 或 UI Turn 的带外通道，最终由硬件安全控制器或 Runtime 看门狗执行。

## 安全与授权边界

### 官方已经提供的边界

- Host 负责连接权限、用户授权、安全策略和 Server 隔离。[架构：Host](https://modelcontextprotocol.io/specification/2026-07-28/architecture#host)
- Tool Server 必须校验输入、实现访问控制、限流并清洗输出；Client 应为敏感操作请求确认、展示输入、校验输出、设置超时和记录审计。[Tools：Security Considerations](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#security-considerations)
- HTTP 授权是可选的；采用时应遵循 MCP 的 OAuth 2.1 授权框架。stdio 不应套用该 HTTP 流程，而应从环境获取凭据。[Authorization：Purpose and Scope](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#purpose-and-scope)
- HTTP Bearer Token 必须出现在每次请求的 Authorization Header 中，不能放在 URL；Server 必须验证 Token 是签发给自身的，不能接受或转发其他服务的 Token。[Authorization：Access Token Usage](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#access-token-usage)
- 官方安全指南建议采用逐步、最小权限的 Scope，敏感操作首次触发时再做针对性的 step-up authorization。[Security Best Practices：Scope Minimization](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices#scope-minimization)
- MCP 是无状态的；显式状态句柄不能当作身份凭证。Server 应在每次请求校验调用者并把句柄绑定到已验证用户。[Security Best Practices：State Handle Hijacking](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices#state-handle-hijacking)

### Robo 仍必须自己实现的安全层（设计推论）

MCP 没有定义设备租约、武装状态、控制权仲裁、速度/力矩/能量上限、工作空间、碰撞检测、安全 PLC、看门狗或急停语义。OAuth Scope 只能说明调用者是否被允许请求某类操作，不证明这个动作在当前物理状态下安全。

建议 Scope 至少分离为 `device:read`、`device:diagnose`、`motion:plan`、`motion:execute`、`device:arm` 和 `device:admin`，并对真实设备执行做 step-up；但最终每次 `robo.execute` 仍要经过 Runtime 的实时状态、租约和策略检查。

## “帮我调通机械臂并叠完衣服”如何穿过这一架构

下面是设计推论，用来验证 MCP 与 HardwareRuntime 的职责是否清晰：

1. Robo Host 连接候选 Adapter Server，并用 `server/discover` 与设备 Manifest 确认版本和能力。
2. `robo.observe` 获取有界场景快照；原始视频和高频传感器流留在感知管线，LLM 只收到选帧、对象和置信度摘要。
3. `robo.plan` 产生“识别衣物 → 抓取角点 → 展平 → 对折 → 放置”的高层计划，不直接产生电机命令。
4. Host 显示将使用的设备、工具头、工作区、速度和失败时停止策略，由用户批准真实执行。
5. `robo.execute` 携带 `device_id`、`lease_id`、`command_id`、截止时间、计划版本和明确约束；Runtime 将步骤转换为厂商轨迹并逐步校验。
6. MCP progress 只更新用户可见阶段；Resource 更新只提示重新读取摘要；真正的闭环控制、碰撞响应和看门狗都在 Runtime/控制器内部。
7. 任一步失败都返回结构化回执和稳定错误类别。Robo 可以修改计划后重试，但不得在未知物理状态下自动重放运动。
8. 用户取消时，Host 同时取消 MCP 请求并调用独立停止路径；只有设备 ACK 或安全控制器证明停止后，UI 才显示安全。

如果换成另一型号机械臂，理想情况下只更换 Adapter Server 与 Manifest；上述高层 Tool、审批、Trace 和 Runtime 安全语义保持不变。

## 本地开发与观察建议

官方 MCP Inspector 是用于测试和调试 Server 的参考工具，提供 Web、CLI 和 TUI 三种客户端，可以查看协议流、列出 Tool、调用 Tool 并验证不同 transport。[MCP Inspector](https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector)

对 Robo 的分层观测建议（设计推论）：

- **协议层**：用 Inspector 检查 discovery、Schema、Tool 调用、Resource 和错误返回；
- **Harness 层**：记录 Agent 决策、审批、Tool 输入输出和重试原因；
- **Runtime 层**：用 OpenTelemetry 记录策略校验、队列等待、SDK 调用、ACK、超时和停止；
- **设备层**：单独记录带设备时间戳的关节、力矩、温度、碰撞和控制器故障；
- **回放层**：按同一个 `run_id` 关联模型、MCP、Runtime 与设备记录，使模拟器能重放现场问题。

Inspector 只能证明 MCP 边界行为正确，不能证明真实机械臂运动安全或时序达标；后者需要模拟器、硬件在环测试、设备日志和独立安全验证。

## MCP 明确不保证什么

以下“协议事实”可直接在官方规范中找到：

- Progress 可以不发，也可以按 Server 自定频率发送。[Progress](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/progress)
- Cancellation 可能被忽略且存在竞态。[Cancellation](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/cancellation)
- Change notification 是 best effort，跨重连可能丢失，应辅以轮询。[Architecture Guide](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture#real-time-updates-notifications)
- 连接断开后订阅要重新建立，stdio 子进程重启时进行中请求丢失。[Subscriptions](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions#cancellation) [stdio Unexpected Termination](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio#unexpected-termination)

以下是基于规范范围与上述事实得出的设计推论，不是官方对机器人控制的明文声明：

- MCP 没有硬实时延迟、抖动、优先级、时钟同步、消息必达或确定性调度保证。
- MCP Tool 不是伺服环，Resource subscription 不是安全级遥测总线，Cancellation 不是急停。
- MCP 的 OAuth、用户确认和 Tool Schema 不能替代机械安全认证、物理联锁、安全 PLC、限位器和独立 E-stop。
- 因此 MCP 可以作为 Robo 的适配器协议和高层命令面，但不能成为唯一控制面或唯一安全面。

## 对现有方案的直接修订建议

1. 保留“MCP 作为首个多厂商接入层”，但不要写成“通过 MCP 直接控制机械臂”。改为“通过 MCP 发现和调用 Adapter，所有运动进入统一 HardwareRuntime”。
2. 新增版本化 `DeviceCapabilityManifest`；不要误用 MCP `capabilities` 表达机械臂硬件能力。
3. 以 `tools + resources + elicitation + progress` 为新实现基线；不要新增对已弃用 Sampling 和 Logging 的依赖。
4. 将 `robo_stop` 与物理急停分开命名和显示；MCP cancellation 只负责取消协议请求。
5. 本地 Adapter 默认 stdio + 沙箱；远程网关使用 Streamable HTTP + OAuth 最小 Scope + Origin 校验。
6. 可观测性采用 Inspector + OpenTelemetry + 设备 Trace；Resource notification 只做刷新提示，完整状态靠重读和 Runtime 真值。
7. 对每个 Adapter 跑同一组契约测试：Manifest、Schema、单位/坐标系、只读观察、执行审批、超时、取消、断线、重复请求、未知状态和停止 ACK。

## 官方资料索引

- [Anthropic：Introducing the Model Context Protocol](https://www.anthropic.com/news/model-context-protocol)
- [MCP 2026-07-28 Specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [MCP Architecture](https://modelcontextprotocol.io/specification/2026-07-28/architecture)
- [MCP Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
- [MCP Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MCP Resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources)
- [MCP Prompts](https://modelcontextprotocol.io/specification/2026-07-28/server/prompts)
- [MCP stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)
- [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [MCP Elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)
- [MCP Progress](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/progress)
- [MCP Cancellation](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/cancellation)
- [MCP Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [MCP Security Best Practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)
- [MCP Deprecated Features](https://modelcontextprotocol.io/specification/2026-07-28/deprecated)
- [MCP Inspector](https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector)
