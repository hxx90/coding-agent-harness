# Robo 硬件 Harness 本地运行手册

## 当前已经能做什么

Robo 现在包含一条可在 Apple Silicon Mac 上运行的硬件开发闭环：

```text
Robo Agent
  -> robo_* 类型化工具
  -> HardwareRuntime（租约、安全仲裁、停止、Trace）
  -> DeterministicSimulatorAdapter 或厂商 MCP Adapter
```

同时提供 CaP-X Agent Bridge：

```text
CaP-X Runner
  -> POST /chat/completions
  -> Robo App Server 会话
  -> 模型生成 Python
  -> CaP-X 隔离执行并反馈 stdout/stderr/图像
```

Mac 本地可以完整验证协议、Agent 多轮修复和确定性设备行为。官方 CaP-X 全量场景仍需要 Linux x86_64/NVIDIA CUDA。

## 一、本地启动 Robo

在仓库根目录执行：

```bash
uv sync --all-extras
uv run robo --workdir .
```

可以从一个可验证的小任务开始：

```text
列出可用 Robo 设备，连接 sim-arm-1，获取租约并显式武装，
用 fold_cloth 折叠 shirt，观察结果，然后停止设备。
```

模型可使用以下工具：

- `robo_devices`：发现、描述、连接、获取租约、武装、解除武装和释放租约；
- `robo_observe`：读取设备状态、当前 run trace 和 Git 工作区状态；
- `robo_execute`：执行一个 Manifest 声明的动作，默认需要批准；
- `robo_sequence`：执行短动作序列，失败时请求停止；
- `robo_stop`：独立请求停止并检查 Adapter ACK，始终允许调用。

确定性模拟器设备 ID 是 `sim-arm-1`。它不是物理仿真器，不模拟碰撞、布料动力学或相机，只用于验证 Harness 的控制语义。

硬件 Trace 默认保存在当前会话目录的 `hardware-runs/<run_id>.jsonl`。关闭会话日志时，保存在项目的 `.vibe/hardware-runs/<session_id>/`。模型通过 `robo_observe` 最多只会看到最新 200 条事件，原始 JSONL 不会全部灌入上下文。

## 二、运行本地自动化测试

运行 Robo 硬件 Runtime、MCP Adapter、工具和 CaP-X Bridge 的聚焦测试：

```bash
uv run pytest \
  tests/core/hardware \
  tests/core/tools/builtins/test_robo.py \
  tests/capx
```

其中本地迷你 Bench 会模拟这个多轮场景：第一次代码引用不存在的变量并收到 stderr；第二次读取旧代码和错误后改成 `fold_cloth("shirt")`，最终完成任务。它使用白名单 AST 解释器，不在 Robo 进程内执行任意模型代码。

## 三、启动 CaP-X Agent Bridge

先确保 Robo 的模型 Provider 和 API Key 已按主 README 配置。Bridge 自己的访问令牌是可选的 `ROBO_CAPX_API_KEY`，它不是模型 Key。

终端一：

```bash
uv run robo-capx-bridge -C . --host 127.0.0.1 --port 8110 --trust
```

健康检查：

```bash
curl http://127.0.0.1:8110/health
```

终端二运行本地 CaP-X 风格烟雾测试：

```bash
uv run robo-capx-smoke \
  --url http://127.0.0.1:8110/chat/completions
```

Bridge 支持 CaP-X 使用的文本消息、`data:image/...;base64,...` 图片和多轮历史，响应字段为 `choices[0].message.content`。每次请求返回 `x-robo-run-id`，Trace 默认位于 `.vibe/capx-runs/`。

如果设置了 Bridge 访问令牌：

```bash
export ROBO_CAPX_API_KEY='自行填写，不要提交到仓库'
```

CaP-X 客户端需要发送 `Authorization: Bearer <token>`。

## 四、接入不同型号机械臂

第一版支持本地 stdio MCP Adapter。厂商 Server 需要实现以下八个 Tool：

- `robo_devices`
- `robo_connect`
- `robo_arm`
- `robo_disarm`
- `robo_observe`
- `robo_execute`
- `robo_stop`
- `robo_disconnect`

在项目 `.vibe/config.toml` 中配置：

```toml
[[mcp_servers]]
name = "robo-ur5"
transport = "stdio"
command = "uv"
args = ["run", "--project", "/path/to/vendor-adapter", "vendor-robo-mcp"]
sampling_enabled = false
disabled_tools = [
  "robo_devices",
  "robo_connect",
  "robo_arm",
  "robo_disarm",
  "robo_observe",
  "robo_execute",
  "robo_stop",
  "robo_disconnect",
]
```

`name` 必须以 `robo-` 开头。八个原始 Tool 必须全部放入 `disabled_tools`，否则 Robo 会拒绝把该 Server 注册成硬件 Adapter。这是为了防止模型绕过 `HardwareRuntime` 的租约、武装、审批、追踪和停止语义直接调用厂商运动接口。

Server 返回的 `DeviceCapabilityManifest` 必须声明设备 ID、厂商、型号、设备类型和支持动作；关节数、单位、坐标系、固件和安全限制放在 `metadata`。MCP 负责高层发现与调用，不负责伺服、看门狗或物理急停。

## 五、接入官方 CaP-X Bench

在 Linux x86_64/NVIDIA 机器上启动 Robo Bridge，然后在 CaP-X 仓库运行最小 S1 trial：

```bash
uv run --no-sync --active capx/envs/launch.py \
  --config-path env_configs/cube_lifting/franka_robosuite_cube_lifting_privileged.yaml \
  --server-url http://<robo-host>:8110/chat/completions \
  --model robo-harness \
  --total-trials 1 \
  --num-workers 1 \
  --record-video False
```

先用 `--use-oracle-code True` 验证 CaP-X 环境自身，再去掉该参数验证 `prompt -> Robo -> Python -> simulator -> reward`。CaP-X 的生成代码必须在独立 worker/容器内执行，不能放入拥有真实硬件权限的 Robo 进程。

## 六、本机 Robosuite 探测结果

2026-09-17 在 Apple M2 Max/macOS 上进行了以下探测：

- CaP-X 官方源码的 `query_model()` 已成功请求 Robo Bridge 的 `/chat/completions` 并读取 `choices[0].message.content`。
- PyPI `robosuite==1.5.2` 搭配默认 MuJoCo 会在 `setup_references()` 触发 joint 类型断言；固定 `mujoco==3.3.7` 后，原生 `Lift/Panda` 已完成 `reset + step`。
- 使用同一兼容组合时，CaP-X 源码的 `FrankaRobosuiteCubeLiftLowLevel` 已完成初始化和 `reset`。它的运动路径调用了定制 fork 才有的 `step(..., skip_render_images=True)`，因此 PyPI Robosuite 不能完成 CaP-X 动作循环。
- CaP-X 固定 Robosuite fork 的 Git 请求持续超时；GitHub tarball 下载到约 281 MiB 后中断且校验损坏，未能安装固定 fork。

所以当前验收结论是：Robo 的本地 HardwareRuntime、CaP-X 协议接入、Robosuite 原生物理步进和 CaP-X 包装器 reset 已通过；CaP-X 的定制运动循环与官方完整 Benchmark 尚未通过。完整研究与外部验收矩阵见 [CaP-X 接入研究](research/capx-bench-integration.md)。
