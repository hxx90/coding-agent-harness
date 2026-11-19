# Mac RGB-D Lift / H1 接入

本轮基线为硬件分支 `3f3c02b525e5c60a5a66c1213c25dd9dbd15df66`，
不是 `src/agent_harness` 的 main。`backup/robo-before-h1-20261117` 保留原提交。
本地实际目录为 `/Users/laihanyu/workspace/Github/mistral-vibe`。

## 启动与开关

在该仓库根目录，使用现有 Python 环境和模型配置：

```bash
uv run --no-sync robo --legacy-harness --workdir .
```

本机已运行过自然语言生成的 Lift 策略及 CLI H1 集成复放。
模型沿用用户已有 `claude-fable-5` 配置，凭据不保存在本文件或例子中。

本次新增的项目 `.vibe/config.toml` 只配置仿真 MCP，继承全局模型。
在**退出当前 CLI 后**切换，重新启动生效：

```bash
# 开启 H1，同时使用 RGB-D 摄像机观测
cp examples/robo/lift-rgbd-h1.toml .vibe/config.toml
# 或关闭 H1，仍使用完全相同 RGB-D 前处理
cp examples/robo/lift-rgbd-off.toml .vibe/config.toml
```

以上适用于本次只有仿真配置的项目文件；后续如自行加了其他项目设置，只编辑
`mcp_servers.env.ROBO_SIM_GEOMETRY_LIBRARY`，不要覆盖整个文件。
取值为 `h1-v8-v9` / `off`。`ROBO_SIM_OBSERVATION_MODE=privileged` 可恢复原真值演示，
但 H1 只允许与 `rgbd` 一起启用。`ROBO_SIM_SEED` 可选；省略则每次随机初态。
`ROBO_SIM_HEADLESS=1` 只关闭 Viewer。它们属于 SimulatorSettings，通过现有 MCP
配置传入；没有新增 VibeConfigSchema 字段，无需改 Settings Manager / Web allowlist。

自然语言示例：

> 仅操作 mujoco-panda-1 仿真，连接、获取租约并武装。读取 Manifest，
> 用受限 Python 策略观察红色方块，通过当前可用的视觉几何能力对准、下降、抓取并抬升。
> 策略内调用 verify("lift_object")，再在策略外独立验证；最后停止、确认 ACK 并释放租约。
> 控制器 delta_xyz 是归一化增量。遵守 200 条执行语句预算，避免过长循环。

任务成功取决于独立的接触和抬升验证。库的 `ok=True` 只表示点云几何可计算。
通用自然语言策略仍可能发生截断/语句超限；当前接入是实验开关，尚未证明收益。

## 原件与权限边界

`vibe/core/hardware/capabilities/h1_v8/` 保留未经格式化的原始 bundle、代码和说明。
manifest 列出来源和文件哈希。bundle SHA-256：
`5ff54ea07319e6e6a069a1274abd4c2f9c7a35ceb795c49f69ddcaaf8675bee8`。

`_geometry.py` 在每次调用前验证 bundle 和导出文件，只有固定函数被普通 Python
导入；模型策略没有 import/eval/exec、文件或网络权限。策略通过
`resting_geometry(points, ...)` 调用；最多 128 个有限数 XYZ 点（米制，±10 m），
返回为 JSON。原说明只在启用 H1 的设备 Manifest 中给模型。
模型动作全部保留原 Runtime 的租约、武装、取消、停止 ACK 和验证门禁。

## 观测与坐标

两组均使用 `sideview` 的 384×384 同一帧 RGB 和深度；默认 Robosuite OpenGL 图像
一起翻转为左上原点。MuJoCo 归一化深度按 near/far 逆投影为相机 optical-Z 米值，
不是欧氏射线长度。K 来自公开相机 fovy；camera_to_world 来自公开相机外参，
旋转修正 `diag(1,-1,-1)`，轴为右、下、前；变换到世界 z-up。
`end_effector_position` 是同一世界坐标系下的机器人本体感知。

`red-rgbd-v1` 取最大红色连通区域，要求 R>45、R>1.5G、R>1.5B，
有效深度 0.05–5 m；确定性均匀采样最多 128 点。基线和 H1 同时得到点云、
可见点均值及 ok/count。它只适用于当前红色立方体 Lift，不是通用物体分割。
没有使用物体真实位置或 segmentation ID。点云物体真值仅在测试的评分器侧用于
坐标校验；`rgbd` actor 状态和动作回执不含 cube_position/grasped/高度/距离等真值。

每次动作后观测重新计算；sequence、时间、标定、单位和原始 NPZ URI 随结果记录。
原始 RGB-D 以唯一文件名保存，避免后续动作覆盖旧帧。独立验证器始终读取新状态。

## 配对 smoke

```bash
uv run --no-sync python -m vibe.hardware_sim.lift_smoke \
  --pairs 3 --seed 117 --prompt-version v1 --output /absolute/new/output-directory
```

`--prompt-version v2` 是保留的短策略契约候选，不改变 token 或执行预算，尚非默认。
每个 trial 一次模型请求、无补跑，temperature=0，输出上限 4096 token；
200 执行语句、50 动作、50 循环、120 秒策略时间。两组交错次序运行。
初始 MuJoCo 状态和点云哈希逐对比较，不同则停止。
这是直接使用同一 Robo provider backend 生成策略的工程测试，不是 CLI 多轮分数，
也不是旧 CaP-X / v9 的复现。H1 和其说明是组间处理因素；模型生成的策略可以不同。
模型输出仍只由 HardwarePolicyRunner 解释。

每次运行保存 protocol、request、response、generated.py、完整 policy JSON、
Runtime JSONL、RGB-D NPZ、RGB PNG、MP4、独立评分、stop ACK、耗时、请求数、
usage 与实际 helper 调用数。输出目录必须全新，旧版本不覆盖。
失败和超时不按退出码算成功；服务错误只记录错误类型，避免凭据泄漏。
本轮文件索引和成绩见工作目录的 `outputs/REPORT.md`。

## main 机制差异

- Vibe 已有受限解释器、循环/语句/动作/墙钟预算、错误停止、租约和 ACK，保留不替换。
- main 的普通 Python 驱动依赖 Linux 隔离；Mac 采用原 AST，未复制 Linux 驱动或合并 main。
- main 的数值传输用于任意 Python actor；本轮只迁移有界 RGB-D/点云行为与相应测试。
- main 的截断恢复尚未迁入 Vibe。smoke 记录 finish_reason 并保留失败源码；
  不执行残缺代码、不通过增加预算或无限重试掩盖问题。
- L1/L2、pick/place、IK、双臂与自动 RSI 都未纳入本轮。
