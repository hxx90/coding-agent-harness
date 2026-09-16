# CaP-X / CaP-Bench 仿真环境与 Robo Harness 接入研究

日期：2026-09-17
证据基线：CaP-X 官方仓库 `main` 提交 [`53e9966d7a8e2fa7494676772bccc35280f5c0ed`](https://github.com/capgym/cap-x/commit/53e9966d7a8e2fa7494676772bccc35280f5c0ed)；论文使用 arXiv v2。本文只引用项目论文、官方仓库、官方源码及底层模拟器官方文档。

## 结论先行

用户所说的 “CaP-X / CaP-X Bench” 可以唯一对应到 [`capgym/cap-x`](https://github.com/capgym/cap-x)：**CaP-X 是总框架，CaP-Bench 是其中的评测组件，不是另一个独立仓库**。CaP-X 还包含 CaP-Gym、CaP-Agent0 和 CaP-RL。[官方 README](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/README.md#L20-L29) [论文](https://arxiv.org/html/2603.22435v2#S1)

对当前只有 Mac 的开发条件，结论分三层：

1. **完整 CaP-X / CaP-Bench 不是官方支持的 macOS 本地运行路径。** README 要求 CUDA GPU，而 `pyproject.toml` 把 uv 环境明确限制为 `linux`、`x86_64`。因此不能承诺在 Apple Silicon Mac 上原生完成官方 benchmark。[安装要求](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/README.md#L34-L50) [uv 平台限制](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/pyproject.toml#L131-L144)
2. **Robosuite/MuJoCo 后端本身可以在 Mac 原生运行。** Robosuite 官方明确支持 macOS 和 Linux、可无 GPU运行；这允许先做“物理后端可启动”的最小烟雾测试，但不等于 CaP-X 全栈已经可用。[Robosuite 官方安装文档](https://robosuite.ai/docs/installation.html)
3. **建议采用 Mac 控制面 + Linux/NVIDIA 仿真执行面的双进程架构。** Robo Harness 在 Mac 上继续负责会话、权限、审批、Trace 和 `HardwareRuntime`；CaP-X 固定版本运行在 Linux x86_64/NVIDIA 主机或云实例。二者先通过 OpenAI-compatible agent endpoint 接通 benchmark，再通过单独的 simulator adapter 接通运行时。这一段是设计建议，不是 CaP-X 官方架构声明。

没有项目身份歧义；主要阻塞是官方平台/GPU约束，而不是仓库或资料不可用。

## 一、项目身份、版本与许可

### 已核实事实

- 官方仓库：<https://github.com/capgym/cap-x>
- 官方项目页：<https://capgym.github.io/>
- 官方论文：[`arXiv:2603.22435v2`](https://arxiv.org/abs/2603.22435v2)
- 顶层代码许可证：MIT，版权声明为 `Copyright (c) 2026 Max Fu`。[LICENSE](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/LICENSE)
- Python 包名和版本：`capx==0.1.0`，Python 范围为 `>=3.10,<3.13`。[pyproject.toml](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/pyproject.toml#L1-L17)
- CaP-X 顶层 MIT 许可证**不能自动代表所有 submodule 和下载的数据集也采用 MIT**。仓库固定了 LIBERO-PRO、Robosuite、Contact-GraspNet、SAM3、cuRobo、BEHAVIOR 等多个外部 submodule，分发前仍需分别核对其许可证与数据集条款。[.gitmodules](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/.gitmodules)

### 官方资料中的数量差异

固定提交的 README 仍写 “39 tasks”，但 arXiv v2 写明 CaP-Gym 共 **187** 个任务，其中 7 个 Robosuite、130 个 LIBERO-PRO、50 个 BEHAVIOR，CaP-Bench 核心对照实验使用其中 7 个环境。[README](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/README.md#L24-L29) [论文任务说明](https://arxiv.org/html/2603.22435v2#S3)

这不是项目身份冲突，而是 README 与论文版本存在漂移。接入时应锁定仓库提交，并把“任务清单版本”和结果一同记录，不能只记录 `CaP-X 0.1.0`。

## 二、CaP-X 实际提供什么

### 已核实事实

CaP-Gym 是分层的 Gymnasium 环境：低层循环是物理模拟器或真实世界，高层是有状态代码执行器。一个 turn 的含义是：Agent 收到观测、生成一段 Python 程序，环境把程序执行完；程序内部可以多次调用感知与控制 primitive。[论文 §2](https://arxiv.org/html/2603.22435v2#S2)

CaP-Bench 的核心 7 个任务是 Cube Lift、Cube Stack、Spill Wipe、Peg Insertion、Cube Re-stack、Two-Arm Lift 和 Two-Arm Handover；每个 tier 每个任务运行 100 个 trial，以 Zero-Shot Pass@1 为主协议。[论文 §3](https://arxiv.org/html/2603.22435v2#S3)

8 个 tier 控制三个变量：primitive 抽象层级、单轮/多轮交互、视觉 grounding：

| Tier | 官方含义 |
| --- | --- |
| S1 | 单轮、高层 primitive、使用无噪声的仿真真值状态 |
| S2 | 单轮、高层 primitive、真实感知模块处理 RGB-D |
| S3 | 单轮、低层 primitive，文档含使用示例 |
| S4 | 单轮、低层 primitive，只给函数签名和 docstring，不给示例 |
| M1 | 多轮，使用 stdout/stderr 执行反馈 |
| M2 | M1 基础上把当前 RGB 图像直接反馈给多模态模型 |
| M3 | 用 VDM 将前后视觉差异转成结构化文本再反馈 |
| M4 | 低层 primitive + 使用示例 + VDM |

来源：[论文 Table 1 与 §3.1–3.2](https://arxiv.org/html/2603.22435v2#S3.SS1)

### 需要特别区分

- **“让 Robo 参加 CaP-Bench”**：Robo 是被测 coding agent，输入 prompt/图像，输出 Python 代码。
- **“让 Robo 使用 CaP-X 仿真环境”**：CaP-X/Robosuite 是 Robo `HardwareRuntime` 下方的模拟设备后端。

前者验证 Agent 的代码生成和迭代能力；后者验证 Robo 的设备生命周期、能力调用、观测、停止和 Trace。两者不能用同一个“接通了”结论代替。

## 三、安装、操作系统与硬件约束

### 已核实事实

| 部分 | 官方要求或事实 | Mac 结论 |
| --- | --- | --- |
| CaP-X 顶层 | README 要求 Python 3.10 与 CUDA GPU；uv 只解析 `linux/x86_64` 环境 | 完整官方安装不支持 macOS；`Operating System :: OS Independent` classifier 与实际 uv 限制矛盾，应以可执行配置为准 |
| Robosuite 路径 | CaP-X 使用其固定的 Robosuite submodule；Robosuite 由 MuJoCo 驱动 | Robosuite 官方支持 macOS/Linux，并可 CPU-only；可做独立 backend smoke test |
| LIBERO-PRO 路径 | 与 Robosuite 1.5 冲突，官方要求另建 Python 3.12 venv，并安装 LIBERO + Contact-GraspNet | CaP-X 没有给出 Mac 支持路径；不要作为第一条本地链路 |
| BEHAVIOR 路径 | 通过 OmniGibson 运行 NVIDIA Isaac Sim，要求 Python 3.10、CUDA 12.x；仓库开发文档还要求 NVIDIA driver 550+ | macOS 不可作为执行机；Isaac Sim 官方当前系统表只列 Ubuntu/Windows 和 NVIDIA RTX GPU |
| 感知服务 | 默认 profile 为 SAM3 + Contact-GraspNet + PyRoKi，文档估算约 5 GB VRAM；full 约 14 GB；minimal 只有 PyRoKi、可 CPU-only | 视觉 benchmark 依赖 CUDA 路径；仅 privileged/oracle 的最小链路有机会避开视觉 GPU服务 |

来源：[CaP-X 安装与 simulator 选择](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/README.md#L34-L107) [perception server profile](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/configuration.md#L50-L87) [BEHAVIOR 前置条件](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/development.md#L80-L127) [Robosuite 安装文档](https://robosuite.ai/docs/installation.html) [Isaac Sim 系统要求](https://docs.isaacsim.omniverse.nvidia.com/latest/installation/requirements.html)

CaP-X 要求递归初始化 submodule。Robosuite 与 LIBERO 的 Robosuite 版本互相冲突，官方明确要求二选一；BEHAVIOR 还会下载机器人、场景、对象和挑战数据并要求接受 NVIDIA 与 BEHAVIOR 条款。[README](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/README.md#L38-L90)

### 设计建议

- Mac 不承担正式 benchmark 执行面。第一阶段远端最低配置建议为 Linux x86_64 + NVIDIA GPU；具体显存按 tier 选择，S2/M2/M3/M4 不低于官方 default profile 的约 5 GB 服务预算，full profile 按约 14 GB 预算，另加 simulator 和模型服务占用。
- LLM 可以继续使用远程 API，不必与 simulator 共卡；本地或远端 vLLM 则另行估算模型显存。
- 固定 CaP-X commit、全部 submodule commit、Python/uv lock、任务 YAML、模型名和服务镜像 digest；否则结果不可比较。
- 不在 Robo 主进程中安装 CaP-X 的整套依赖。Robo 当前是 Python 3.12+ 应用，而 CaP-X 的主路径、LIBERO 路径与 Isaac Sim 路径有不同 Python/依赖组合，进程隔离比依赖合并可靠。

## 四、模拟器、任务、观测与动作接口

### 4.1 后端

已核实的三类仿真后端：

- Robosuite：MuJoCo，覆盖 7 个核心 tabletop/bimanual benchmark 任务。
- LIBERO-PRO：基于其固定版本 Robosuite 的任务套件。
- BEHAVIOR：OmniGibson + NVIDIA Isaac Sim，R1 Pro 移动操作任务。

仓库还包含 Franka 真实机器人低层实现，但它不属于本次 Mac 仿真最小链路。[模拟器注册表](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/simulators/__init__.py)

### 4.2 低层 Gymnasium 接口

所有模拟器 wrapper 继承 `BaseEnv`，必须实现：

```python
reset(*, seed=None, options=None) -> (observation, info)
step(action) -> (observation, reward, terminated, truncated, info)
get_observation() -> observation
compute_reward() -> float
task_completed() -> bool
```

低层 `action` 是关节位置、夹爪位置等控制量；环境通过名称注册到工厂。[BaseEnv 源码](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/base.py#L11-L95)

自定义环境的视觉观测约定至少包含：

```text
obs["robot0_robotview"]["images"]["rgb"]    (H,W,3) uint8
obs["robot0_robotview"]["images"]["depth"]  (H,W,1) float32
obs["robot0_robotview"]["intrinsics"]        (3,3) float64
obs["robot0_robotview"]["pose_mat"]          (4,4) float64
```

来源：[Adding Environments](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/adding-environments.md#L3-L51)

### 4.3 Coding-agent 接口

`CodeExecutionEnvBase` 是高层 Gymnasium 环境：

- action space 是 Python 源码字符串 `spaces.Text(max_length=4096)`；
- reset 观测由低层观测加 `full_prompt` 组成；
- prompt 自动拼接所启用 API 函数的签名和 docstring；
- `step(code)` 执行代码后返回标准 Gymnasium 五元组；
- `info` 至少包含 `sandbox_rc`、`stdout`、`stderr`、`task_prompt`、`task_completed`；
- reward 委托给低层环境，当前默认以 `reward == 1.0` 判定 terminated。

来源：[代码环境初始化和 prompt](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/tasks/base.py#L36-L151) [观测与 step](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/tasks/base.py#L227-L298)

执行上下文会直接暴露 `env`、`APIS`、`INPUTS`、`RESULT`、当前 `obs` 和全部 API helper。命名空间跨 turn 保留、reset 后清空。[代码执行上下文](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/tasks/base.py#L153-L205)

这里的所谓 sandbox 实际是同一 Python 进程内的 `exec`，允许任意已安装包 import；开发文档也只称其为 “safe-ish”，建议更强隔离时使用 Docker 或 nsjail。[执行器源码](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/tasks/base.py#L58-L82) [已知问题](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/development.md#L137-L140)

**设计建议：不要让 CaP-X 生成代码在 Robo Host/App Server 进程内执行。** 即使只是仿真，也应把每个 trial 放在可丢弃容器或受限 worker 中，并限制网络、文件系统、CPU、内存、运行时间和输出量。

## 五、官方 benchmark 调用与输出

### 已核实事实

标准单轮 Robosuite 评测：

```bash
uv run --no-sync --active capx/envs/launch.py \
  --config-path env_configs/cube_stack/franka_robosuite_cube_stack.yaml \
  --model "google/gemini-3.1-pro-preview"
```

官方默认示例是 100 trials、12 workers；Web UI 使用同一个入口加 `--web-ui True`，默认监听 `http://localhost:8200`。[README Quick Start](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/README.md#L145-L180) [LaunchArgs](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/launch.py#L37-L123)

YAML 选择环境、低层模拟器、privileged 状态和 API 集合，并设置视频、输出目录、trial 数和 worker 数；CLI 可以覆盖这些字段。[配置格式](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/configuration.md#L3-L48)

官方 `quick` regression 不是无依赖单测：它跑 10 个 cube-stack trials、5 workers，并要求 LLM proxy、SAM3、GraspNet、PyRoKi 服务可用。因此它不适合作为只有 Mac 时的第一条测试。[regression_test.sh](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/scripts/regression_test.sh#L1-L29) [服务与 quick 模式](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/scripts/regression_test.sh#L131-L203)

每个 trial 会保存生成代码、stdout/stderr、reward、task completion、响应记录和可选视频；批量运行汇总后写完成标志。[trial 日志字段](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/trial.py#L72-L105) [runner 输出](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/envs/runner.py#L122-L165)

## 六、可供外部 Harness 使用的扩展点

### 已核实事实

1. **外部 Agent/模型端点**：Runner 会向 `--server-url` POST OpenAI chat-completions 风格 JSON，请求含 `model`、`messages`、温度和 token 限制；普通响应读取 `choices[0].message.content`。这是现成且最小的外部 Agent 接缝。[LLM client](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/llm/client.py#L180-L273)
2. **新模拟器**：实现五个 `BaseEnv` 方法并 `register_env(...)`。[Adding Environments](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/adding-environments.md#L3-L51)
3. **新任务**：继承 `CodeExecutionEnvBase`，定义 prompt/oracle code，注册执行环境，并由 YAML 组合低层环境和 API。[Adding Environments](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/adding-environments.md#L53-L128)
4. **新机器人 API**：继承 `ApiBase`，`functions()` 返回暴露给生成代码的函数映射；函数签名和 docstring 就是模型接口文档。[Adding APIs](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/adding-apis.md#L1-L75)
5. **外部 perception/control 服务**：YAML 的 `api_servers` 可启动或复用固定端口服务；如果端口已占用则跳过启动。[Configuration](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/configuration.md#L50-L87)

官方配置文档还提到 `capx/serving/providers/` 的 `generate_code` provider 扩展，但固定提交中没有该目录，实际 `client.py` 是直接 HTTP POST。因此首版接入不应依赖这个文档中尚不存在的 provider 插件点，应采用已存在的 `--server-url` 接口。[配置文档](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/configuration.md#L89-L117) [实际 serving 目录](https://github.com/capgym/cap-x/tree/53e9966d7a8e2fa7494676772bccc35280f5c0ed/capx/serving)

## 七、推荐的 Robo 接入方案

以下全部是基于上述接口的**设计建议**。

### 路径 A：先把 Robo 作为 CaP-Bench 的被测 Agent

这是最小、最可比的接入：

```text
CaP-X Runner（Linux/NVIDIA）
  └─ POST /chat/completions
       └─ Robo CaP Agent Adapter（Mac 或远端）
            └─ Robo Harness / 模型 / 会话与 Trace
                 └─ 返回 Python code
```

实现一个很薄的 `robo-capx-agent` HTTP adapter：

- 接收 CaP-X 的 `model`、`messages`、图像 data URL 和采样参数；
- 创建隔离的 benchmark session/turn；
- 禁止 Robo 自己调用真实硬件工具，只允许生成最终 Python 源码；
- 返回 `choices[0].message.content`；
- 将 CaP trial id、Robo session id、模型调用、生成代码和 CaP 输出目录关联到同一个 `run_id`。

运行 CaP-X 时使用一个不触发其 OpenRouter 特判的自定义 model 名，例如 `robo-harness`：

```bash
uv run --no-sync --active capx/envs/launch.py \
  --config-path env_configs/cube_lifting/franka_robosuite_cube_lifting_privileged.yaml \
  --server-url http://<robo-host>:<port>/chat/completions \
  --model robo-harness \
  --total-trials 1 \
  --num-workers 1 \
  --record-video False
```

先跑 S1 privileged；随后依次扩展到 S2、S3/S4、M1，最后才是需要视觉反馈/VDM 的 M2–M4。这样可以把代理协议问题、运动 API 组合问题和视觉服务问题分开定位。

### 路径 B：再把 CaP-X 仿真接到 Robo `HardwareRuntime`

推荐独立 sidecar，而不是 import 到 Robo 主进程：

```text
Robo Tools / App Server
          │
    HardwareRuntime
          │ 统一设备契约、租约、审批、限位、Trace
    CaPXSimulatorAdapter
          │ 版本化 RPC；禁止任意 exec
  CaP-X sidecar（Linux/NVIDIA）
          │
  Robosuite / LIBERO / BEHAVIOR
```

adapter 最小映射：

| Robo 能力 | CaP-X 对应物 | 额外约束 |
| --- | --- | --- |
| `discover/describe` | 固定 YAML + 环境/API 注册表 | 返回 simulator、task、seed、坐标系、单位和 commit |
| `connect` | 实例化环境并 `reset(seed=...)` | 一个 lease 对应一个 episode；禁止共享可变环境 |
| `observe` | `get_observation()` | 图像走对象存储或流，不塞入无界 JSON；记录 simulator step |
| `execute` | 调用 allowlist API 或低层 `step(action)` | 不直接暴露 CaP-X 的任意 Python `exec` 给通用硬件工具 |
| `stop` | 中断 worker、停止推进仿真并封存 episode | 这是仿真停止语义，不冒充真实急停 |
| `release` | 关闭环境/worker | 丢弃 lease，保存 episode artifact |

CaP-X 的 API 函数可以作为 adapter 内部实现参考，但 Robo 对外仍使用自己的类型化 `CommandEnvelope`、租约、安全状态和回执。benchmark 的 code-string action 只存在于隔离评测路径，不能成为真实机械臂公共动作协议。

### 不建议的路径

- 不把 `capx` 整包加进 Robo 的 Python 依赖图；平台、Python 和 Robosuite/LIBERO 版本冲突会污染主应用。
- 不让 CaP-X 的 `SimpleExecutor` 承担 Robo 安全沙箱职责。
- 不把 `task_completed` 或 `reward == 1.0` 当作 Robo 的安全状态；它们只是任务指标。
- 不通过 Docker Desktop 在 Mac 上宣称完成 CUDA benchmark；Mac 容器没有 NVIDIA CUDA passthrough。

## 八、Mac 上最小可执行烟雾测试

### 目标和边界

Mac 上最小、诚实的测试是：**先用 PyPI Robosuite 的兼容版本完成 MuJoCo `Lift/Panda` 的 reset 和零动作 step，再尝试 CaP-X 自己的包装器**。它验证 Mac 能运行核心物理后端及动作/观测循环；它不等于固定 fork、感知服务、代码执行器、CaP-Bench 评分或多轮 Agent 全部可用。

Robosuite 官方支持 macOS；Mac 的交互 viewer 需要 `mjpython`，但下面关闭屏幕与离屏渲染，因此只验证动力学。[Robosuite 官方安装文档](https://robosuite.ai/docs/installation.html) [CaP-X 固定的 Robosuite commit](https://github.com/uynitsuj/robosuite/tree/97292732ed909ac3ae116579fb768607034a4dbd)

```bash
env -u MUJOCO_GL uv run --no-project --python 3.12 \
  --with 'robosuite==1.5.2' \
  --with 'mujoco==3.3.7' \
  --with numpy \
  python - <<'PY'
import numpy as np
import robosuite as suite

env = suite.make(
    env_name="Lift",
    robots="Panda",
    has_renderer=False,
    has_offscreen_renderer=False,
    use_camera_obs=False,
)
obs = env.reset()
low, _high = env.action_spec
action = np.zeros_like(low)
obs, reward, done, info = env.step(action)
print({
    "observation_keys": sorted(obs)[:8],
    "action_dim": int(action.size),
    "reward": float(reward),
    "done": bool(done),
})
env.close()
PY
```

2026-09-17 实测通过：`action_dim=7`，一次 step 返回有限 reward 且无 MuJoCo 初始化错误。默认解析到的更新版 MuJoCo 会触发 Robosuite 关节类型断言，因此这里明确固定 `3.3.7`。

进一步使用 CaP-X 源码中的 `FrankaRobosuiteCubeLiftLowLevel` 验证时，环境初始化和 `reset` 成功，但第一次动作调用失败：PyPI Robosuite 的 `MujocoEnv.step()` 不支持 CaP-X 定制 fork 增加的 `skip_render_images` 参数。固定 fork 的 Git 请求在本机网络环境持续超时。因此该结果证明了 Mac 上的底层动力学和 CaP-X reset 路径，不证明完整 CaP-X 动作循环已通过。

### 第一条真正的 CaP-X 端到端烟雾测试

移到 Linux x86_64/NVIDIA 执行机后分两步：

1. 用 `franka_robosuite_cube_lifting_privileged.yaml`、`--use-oracle-code True --total-trials 1 --num-workers 1 --record-video False` 验证 CaP-X 环境和 PyRoKi；此时不经过 Robo。
2. 去掉 `--use-oracle-code`，把 `--server-url` 指向 `robo-capx-agent`，仍只跑 1 trial；验证 prompt → Robo → code → simulator → reward/artifact 的完整闭环。

只有第二步成功，才可以说 “Robo Harness 已接入 CaP-Bench”；只有路径 B 的 adapter 契约测试通过，才可以说 “Robo HardwareRuntime 已使用 CaP-X 仿真环境”。

## 九、建议验收矩阵

| 阶段 | 环境 | Case | 通过标准 |
| --- | --- | --- | --- |
| 0 | Mac | 上述 PyPI Robosuite 单步 smoke | **已通过**：reset/step 成功，无 GPU |
| 0.5 | Mac | CaP-X Franka 包装器 + PyPI Robosuite | **部分通过**：reset 成功；动作循环需要固定 fork |
| 1 | Mac | Robo 的 deterministic `SimulatorAdapter` 契约测试 | **已通过**：discover/connect/lease/execute/observe/stop 全通过 |
| 1.5 | Mac | CaP-X `query_model()` → Robo Bridge | **已通过**：代码响应可被官方客户端解析 |
| 2 | Linux/NVIDIA | CaP-X privileged oracle，1 trial | 环境、PyRoKi、reward 和 artifact 完整 |
| 3 | Linux/NVIDIA + Robo | Robo Agent endpoint，S1 单 trial | CaP 能解析响应、执行代码、结果绑定同一 `run_id` |
| 4 | Linux/NVIDIA + Robo | S1 批量固定 seed | 成功率与原始输出可复现，失败可回放 |
| 5 | Linux/NVIDIA + Robo | S2/S3/S4/M1 | 分别验证视觉、低层 primitive 和 stdout/stderr 自修复 |
| 6 | Linux/NVIDIA + Robo | M2–M4 | 图像/VDM、有界上下文和多轮预算生效 |
| 7 | Linux/NVIDIA + Robo Runtime | `CaPXSimulatorAdapter` 契约与故障注入 | 超时、worker 崩溃、观测过期和 stop 都产生确定性回执 |

## 十、对当前实现计划的直接影响

1. 继续先完成 Robo 自己的完整 `HardwareRuntime`、确定性 simulator、record/replay 和安全契约；不要让 CaP-X 的平台问题阻塞核心实现。
2. 同时预留两个边界清楚的模块：`CaPBenchAgentEndpoint` 与 `CaPXSimulatorAdapter`。前者评测 Agent，后者提供模拟设备；不要合并。
3. CI 分层：Mac CI 跑纯 Harness 与 Robosuite backend probe；CaP-X E2E 标成 Linux/NVIDIA 专用 job。
4. CaP-X worker 必须进程/容器隔离，并以固定 commit 和 seed 启动；Robo 主进程只消费受限的观测、回执和 artifact 索引。
5. 在开始实现 CaP-X bridge 前，先确认可用的 Linux/NVIDIA 执行机。若没有，则本阶段的可交付上限是 Mac backend smoke + bridge contract test，不能声称完成官方 benchmark。

## 十一、已知阻塞与风险

- **硬阻塞**：只有 Mac 时无法按官方配置跑完整 CaP-X/CaP-Bench；BEHAVIOR/Isaac Sim 明确不是 macOS 路径。
- **依赖风险**：顶层 uv 限制、三个 simulator 家族、多个 editable submodule 和不同 Robosuite/Python 版本要求，使“单环境全装”不可靠。
- **安全风险**：生成代码通过同进程 `exec` 执行，不能放进 Robo Host 或连接真机的高权限进程。
- **协议风险**：CaP-X 对外 agent seam 是 OpenAI-compatible HTTP，不是 MCP/ACP；需要一个明确的翻译 adapter。
- **可复现风险**：README 的任务数量已与论文 v2 不一致；必须记录 commit、submodule、config、seed、模型与服务版本。
- **指标风险**：CaP 的 reward/task completion 衡量任务完成，不覆盖 Robo 的租约、审批、停止确认、看门狗与物理安全语义。

## 来源索引

- [CaP-X 官方仓库](https://github.com/capgym/cap-x)
- [CaP-X 官方论文 v2](https://arxiv.org/abs/2603.22435v2)
- [CaP-X README（固定提交）](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/README.md)
- [CaP-X pyproject（固定提交）](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/pyproject.toml)
- [CaP-X 环境扩展文档](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/adding-environments.md)
- [CaP-X API 扩展文档](https://github.com/capgym/cap-x/blob/53e9966d7a8e2fa7494676772bccc35280f5c0ed/docs/adding-apis.md)
- [Robosuite 官方安装文档](https://robosuite.ai/docs/installation.html)
- [MuJoCo 官方 Python 文档](https://mujoco.readthedocs.io/en/stable/python.html)
- [NVIDIA Isaac Sim 官方系统要求](https://docs.isaacsim.omniverse.nvidia.com/latest/installation/requirements.html)
