# Robo 0.1 验收结果

日期：2026-09-15；macOS arm64，Python 3.11.10。所有结果均为模拟设备/确定性协议测试，未连接真实 MHS 或真实模型。

## 测试

- 最终源码全量：**208 passed in 76.27s**（184 项既有测试 + 24 项 Robo 测试，无跳过）。[完整输出](evidence/pytest-full.txt)
- 恢复摘要最终补丁：Robo 多模态/会话测试及旧 CLI 会话测试 **25 passed in 16.24s**。[输出](evidence/recovery-final.txt)
- 两路审查修复后的 Robo 与旧实验引擎定向回归：**51 passed in 55.21s**。[输出](evidence/review-regressions.txt)
- Ruff 检查通过；30 个文件格式检查通过；Mypy 检查 27 个源码文件通过。[输出](evidence/static.txt)

覆盖设备发现、像素观察、本地程序反馈、失败后编辑重跑、独立物理验证、动作回执丢失与去重/核对、跨 job 未知动作约束、共享工作单元互斥、源文件/依赖与版本隔离、运行 artifact 篡改拒绝、断开重连与提交 ID 去重、模型结束/取消与程序生命周期独立、程序取消及设备确认、超时/CPU设置/RSS/日志预算、过期观察、沙箱读写/网络/fork限制、宿主 SIGKILL 后真实 worker PID 退出、宿主重启不重放、长轮询断开不续租、模型预算及时打断长轮询、每次推理前现场摘要及丢回执恢复摘要、图像存储/携带媒体导出导入/压缩/provider HTTP传输、旧文本 schema 兼容。

CPU 使用 OS RLIMIT_CPU；测试不构成所有平台上的资源隔离认证。RSS 只是采样停止阈值，无法证明峰值永远低于阈值。观察与验证只覆盖本模拟工作单元。

## 安装与命令验证

wheel 和 sdist 构建成功。[构建输出](evidence/build.txt)

将最终 wheel 安装到干净 `/tmp/robo-wheel-check-20260915` venv，确认：

- `agent-harness --version` → `2.0.0`。
- `robo --version` → `Robo 0.1.0`。
- 未安装 Streamlit，两个 CLI 入口可用。[安装输出](evidence/wheel-install.txt)
- 使用安装后的旧 CLI 执行真实 loopback HTTP 编程任务：读取 `counter.py`，修改 `value = 1` 为 `value = 2`，实际 unittest 验证通过，3 次模型协议请求。[结果](evidence/cli-smoke.json)
- 使用安装后的 Robo 启动独立宿主，执行 `robo demo`，`passed=true`。[结果](evidence/demo.json)

## 演示结果

1. 第一版方向错误，Python 正常退出（0），独立物理验证 **failed**，误差约 21.63 mm。
2. 现场修改产生新 ProgramVersion，再运行本地 RGB 感知/反馈程序；Python 正常退出（0），独立物理验证 **succeeded**，3 个动作后样本的最大误差约 **0.18 mm**。
3. 第二版首次动作故意丢回执，先查询原 command ID 并重新观察，核对 confirmed 后继续；没有重复移动。两次任务均通过新客户端 lookup/subscription 完成跟踪。

[第二版完整时间线](evidence/timeline.json)，[失败后的画面](evidence/before.png)，[修正后的画面](evidence/after.png)。媒体原件、动作、异常和日志可通过记录中的 job/version/observation IDs 对照。

## 真实集成状态与阻塞

- **真实 MHS：未集成。** 官方仍为申请制研究预览，没有本次可取得的 SDK/规范发行版；`host --backend mhs` 明确报错，不回退假装实机。来源与版本状态见 [MHS 核查](01-mhs-research.md)。
- **真实硬件：未验证。** 没有设备、驱动、标定或设备级停止/保护链路；模拟结果不代表实机可靠性或性能。
- **真实模型：未验证。** 最终检查本机 base URL、model、API key 均未配置。确定性 model+工具闭环和实际 HTTP 图像传递已验证，不能据此宣称真实模型理解、编程或视觉能力通过。
- **范围限制：** 本地 macOS、单用户/单模拟工作单元；无 Linux worker 沙箱、原生视频协议或硬实时保证；依赖只支持固定 stdlib 和清单内 vendored Python。整个可信 shell 环境的真实硬件隔离仍待实机集成设计。

[可运行命令与操作流程](03-user-guide.md) · [两路审查及修复](06-code-review.md)
