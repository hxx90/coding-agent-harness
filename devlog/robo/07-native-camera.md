# 真实相机接入与验收（2026-09-15）

## 范围与实现

用户要求：用真实的 camera 作为设备接入并测试。本轮在 `ea25608` 的 Robo 运行时之上接入本机 **FaceTime 高清相机**；没有选择 OBS Virtual Camera，也没有把桌面截图作为摄像头。

- 驱动：`robo-avfoundation/0.1`，通过 macOS 原生 AVFoundation 发现设备、按 UID 打开视频输入，Core Image 转换 RGB。Swift 桥接源码随 wheel 打包，首次使用由 `swiftc` 编译，按源码 SHA-256 缓存；Python 不需要 PyObjC/OpenCV/FFmpeg。初始硬件探测另用 FFmpeg 7.1 成功取得过真实画面。
- 原始采集尺寸由设备决定，本机实测 1920×1080；给程序/模型的预览为 320×180，约 5 帧/秒。只保留最新帧于内存，调用观察时才持久化 PNG。没有音频采集。
- 设备 `native-camera` 只允许 `observe`，没有运动能力；模拟器仍是 `sim-camera` / `sim-stage`。宿主按适配器选择权限、资源和验证器。
- 相机源 UID、采集会话、原始 PTS、映射后的采集时间、接收时间、图像坐标原点、驱动源码哈希与图像引用随观察持久化。关联 job 和 ProgramVersion。若设备时钟无法映射，时间类型明确降为接收时间估计，不能用于授权提交或证明采集新鲜度；已知过期时间戳直接保留并拒绝。内参、外参均未知，不宣称有空间标定。
- agent 的 `robo_observe` 与冻结程序的 `robo.observe()` 共用宿主接入。程序可读取真实 RGB 并在本地计算亮度等反馈，无须等待远程模型。
- `--home` 固定到 backend + 设备 UID，拒绝把旧模拟任务解释成真实任务，或把另一个相机当成原设备。
- 取消任务撤销该程序的相机权限；宿主仍持续采集，供后续观察。退出宿主释放相机。`protect` 释放相机并持久化保护状态，重启不会清除；如需新的独立实验，使用另一个 home。
- 宿主被强杀后，独立父进程管道的 EOF 使原生采集进程与 worker 退出。恢复宿主只将旧任务标为 interrupted，不重放程序。
- 代码正常结束、采集验证成功和物理场景目标达成分别记录：相机后验验证检查 3 个独立新帧；未配置场景判据时 `physical_verification.status` 仍是 `unknown`，`capture_verification.status` 可以是 `succeeded`。

真实图片放在 `~/.robo-camera/` 等本机私有目录，未加入 Git。宿主本身不上传相机媒体；以后运行带 Robo 工具的模型任务时，观察图像会按现有 provider 多模态流程发送给配置的模型服务。

## 可运行命令

环境：macOS、Python 3.11+、Xcode Command Line Tools（`swiftc`）；第一次使用需允许启动终端/应用访问相机。当前机器已获得授权。项目的 `.venv` 已安装依赖。

```bash
cd /Users/laihanyu/workspace/Github/coding-agent-harness
# 不打开相机，仅列出可选择的设备
.venv/bin/robo --home "$HOME/.robo-camera" cameras

# 终端 A：启动独立宿主；默认选唯一的内置摄像头
.venv/bin/robo --home "$HOME/.robo-camera" host --backend camera
# 指定设备时，在上条命令后追加 --camera '设备 UID 或完整名称'
```

另一个终端：

```bash
cd /Users/laihanyu/workspace/Github/coding-agent-harness
.venv/bin/robo --home "$HOME/.robo-camera" devices
.venv/bin/robo --home "$HOME/.robo-camera" observe --output "$HOME/.robo-camera/manual-preview.png"
.venv/bin/python scripts/robo_camera_smoke.py --home "$HOME/.robo-camera"
```

输出图片必须是新文件；重复运行观察时换一个文件名。验收脚本自动执行：发现 → 观察 → 发布版本 1 → 提交并改动工作区 → 断开/重新查询 → 5 次本地取帧和日志 → 发布版本 2 并重跑 → 后验新帧验证 → 取消持续取帧程序并确认 → 将真实图片转换成 provider 图像请求格式（仅本地序列化）。示例在 `examples/robo/camera/`。脚本输出报告包含任务 ID、版本、采样记录和预览的绝对路径。

可用 `robo ... events JOB_ID --follow` 订阅日志；`robo ... cancel JOB_ID` 取消任务；`robo ... status JOB_ID` 检查结果。终端 A 按 Ctrl+C 退出宿主并释放相机。旧 `robo demo` 仍只用于模拟 XY 对准，真实相机宿主会明确拒绝该演示。

模型凭据配置好之后，可使用 `robo --home "$HOME/.robo-camera" run '观察相机并编写一个本地采样程序，记录五帧的亮度'`。本轮没有可用模型凭据，未声称完成远程模型视觉推理测试。

## 验证

- 真实 FaceTime 相机：两版程序各取 5 帧，退出码均为 0；后验采集验证成功，物理目标状态保留 unknown。
- 真实运行中版本隔离、重新连接、稳定 request ID 查询和取消确认已通过。
- 真实图像已人工检查方向；媒体引用到 provider 图像格式的本地转换通过。
- 真实宿主退出、宿主强杀、设备保护及重启保护状态：结果见 `evidence/camera-lifecycle.json`。
- 新增 9 项自动化测试使用明确的协议测试替身，覆盖原始像素通路、权限、设备绑定、过期帧、权限拒绝、采集丢失、父进程 EOF、取消与保护。它们不作为真实硬件证据。
- 全量回归：217 passed；Ruff 与 Mypy（29 个源码文件）通过；真实生命周期检查通过。最终 wheel 独立安装、随包 Swift 编译/发现真实设备通过；旧 CLI 的本地 HTTP 协议/编辑/验证冒烟测试通过（`evidence/camera-wheel.json`）。两路独立审查及修复见 `08-camera-review.md`。

真实采样元数据见 `evidence/camera-smoke.json`；真实图片未纳入仓库。此接入是原生相机集成，**不是 MHS 集成**。MHS 的公开 SDK/规范与预览硬件条件仍未满足；本轮也没有增加运动设备、空间标定或硬实时控制保证。

验收结束时已退出所有本轮相机宿主并确认相机采集进程释放。`~/.robo-camera/` 保留任务/图像/驱动缓存，未设置保护锁；上面的启动命令可直接重新运行。保护测试使用另一个私有临时 home。
