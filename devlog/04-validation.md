# 验证与验收记录

环境：2026-09-15，macOS arm64，Python 3.11.10。开发解释器为 `/private/tmp/coding-agent-harness-check-20260914/bin/python`。测试使用真实临时目录、子进程、HTTP/SSE 服务与 PTY。外部模型仅由确定性协议服务替代，实际文件工具、会话、网络客户端和命令执行均运行生产实现。

## 全量测试结果

`python -m pytest -q -o addopts= --durations=5` 最终结果：**184 passed in 22.30s**，无跳过。包括 67 项原有测试和 117 项 CLI 测试。最终输出：[evidence/pytest-final.txt](evidence/pytest-final.txt)。

首次全量为 163 passed in 19.87s，记录在 [evidence/pytest.txt](evidence/pytest.txt)。随后独立代码审查发现异常边界，修复并补充 21 项测试后重新跑了全部测试。

此前基线为 57 passed / 10 failed。原有失败集中于 Homebrew Python 在 macOS 沙箱中解析并启动 Framework 解释器。此次修复后，原有 67 项全部包含在通过的全量测试中，没有删掉或跳过失败测试。

类型检查、Ruff 静态检查与格式检查通过。源码分发包和 wheel 构建成功。实际输出见：

- [mypy.txt](evidence/mypy.txt)
- [ruff.txt](evidence/ruff.txt)
- [format.txt](evidence/format.txt)
- [build.txt](evidence/build.txt)
- [install.txt](evidence/install.txt)
- [install-smoke.txt](evidence/install-smoke.txt)

审查所报问题全部修复并经原审查 Agent 独立复核。Standards 复核相关 38 项通过，Spec 复核相关 13 项通过；另一次本地集中验证 42 项通过，输出见 [review-regressions.txt](evidence/review-regressions.txt)。两路报告与处理记录见 [06-code-review.md](06-code-review.md)。

## R01–R14 验收矩阵

| 要求 | 实现 | 实际验证证据 |
| --- | --- | --- |
| R01 CLI/交互/stdin/JSONL/退出码 | `cli.py`、console script | `test_cli.py` 使用安装后的命令，涵盖帮助、版本、stdin、JSONL、错误退出码与真实 PTY 连续两轮交互 |
| R02 配置/凭据/模型/诊断 | `settings.py`、auth/config/models/doctor | 配置优先级、错误配置、隐藏凭据与不破坏结构的字段脱敏、模型列表、诊断、auth 文件 0600；跨 SSE chunk 的 Key 不出现在事件和导出 |
| R03 HTTP/SSE 与失败恢复 | `provider.py` | JSON fallback、UTF-8 文本、流式工具参数/usage/元数据、重复 ID、畸形响应、提前结束、不重放已输出文本、有限重试、HTTP 头与响应体阶段取消、2 MiB 流限制 |
| R04 连续 Agent 执行/预算 | `CodingSession.run()` | 真实 HTTP 服务驱动读取→编辑→失败验证→修复→成功→恢复；工具错误反馈、重复操作保护、请求/Token/时间预算 |
| R05 编码工具 | `ToolSet`、`Workspace` | 实际文件 read/glob/grep/write/edit/apply_patch，Shell stdout/stderr/退出码/超时/环境，固定验证和待办 |
| R06 权限与 Plan | `Permissions` 和 Workspace 路径规则 | Plan 即使放行全部也不执行写入；非交互停止、具体修改预览、精确操作授权不跨变更重用；路径穿越、敏感路径、符号链接、保护配置与 ./ /绝对路径别名 |
| R07 持久化与恢复 | `SessionStore`、调用前持久化 | 跨 CLI 进程继续、跨进程锁、损坏会话隔离、导入不带恢复权限、文件大小上限保留旧状态、未确认工具恢复后不重放 |
| R08 Diff/撤销/重做 | Workspace 快照与 Session 轮次 | 多轮与跨实例撤销重做、外部冲突整体拒绝、被明确编辑的忽略文件、空文件/权限位 Diff、CRLF 保留；模型等待期间的外部修改不进入撤销 |
| R09 指令/附件/压缩 | `context.py`、Workspace.instructions | 根与嵌套 AGENTS.md 顺序、敏感指令不读取、附件不越项目、指令不授予权限；压缩保留最新要求与完整 call/result 配对，完整历史仍保留 |
| R10 取消与进程回收 | HTTP async cancellation、POSIX 进程组 | HTTP 头/体取消；PTY 中发送 SIGINT 取消权限输入和模型并继续下一轮；PTY 中取消 Shell 保留已写文件；超时回收父进程已退出但仍持有管道的子进程 |
| R11 真实验证语义 | 最终状态前的宿主验证 | 首次验证失败继续修复后通过；验证期间写入工程不能记为通过；纯问答无需修改；未配置验证准确显示 not_configured |
| R12 Skills/子会话/MCP | `extensions.py` | 临时 Skill 正文与资源；受限独立子会话及父预算计入；真实 MCP 子进程 initialize/list/call、启动与调用两次授权、协议错误和超时 |
| R13 OpenCode 源码参考 | 固定 SHA 与设计取舍 | [01-opencode-reference.md](01-opencode-reference.md) 的固定提交链接；已获取文件的 SHA-256 清单 |
| R14 文档/检查/安装/旧回归 | 本目录、README、测试与构建 | 最终全量 184 项、类型/静态/格式检查、构建与独立安装记录；[06-code-review.md](06-code-review.md) 记录审查及整改 |

## 干净安装与简单任务

独立虚拟环境只安装 wheel 及声明的运行依赖，检查 `importlib.util.find_spec('streamlit') is None`，再通过实际安装的 `agent-harness` 命令执行冒烟任务。

可重复脚本：`python scripts/cli_smoke.py --executable /path/to/agent-harness`。它创建临时项目 `counter.py`，模型协议服务依次要求 read、edit，然后由宿主运行真实 `unittest`；目标是把 `value = 1` 改成 `value = 2`。脚本断言终态 completed、验证 passed、磁盘内容正确，保存 JSONL 事件并输出工作区位置。

最终 wheel 已在该独立环境重新安装并跑通冒烟；安装环境与冒烟结果见上方 install-smoke.txt。仓库内也准备了 `.venv` 开发环境，可使用 `.venv/bin/agent-harness`，对应输出见 [local-environment.txt](evidence/local-environment.txt)。模型协议服务使用确定性响应，不能作为模型自主编码能力证据。

## 证据边界

- 未发现本任务可用的真实模型服务配置和 Key，没有真实模型端到端能力验收，也没有取用其他项目或账号的密钥。
- macOS 上的功能与 POSIX PTY 已实测。Linux/Windows 尚未进行端到端运行认证；Windows 进程终止只覆盖直接子进程。
- Shell/MCP 的外部副作用不属于文件快照恢复保证；没有声称 CLI 运行在 OS 沙箱里。
- 上下文采用有界确定性检查点，可能舍弃早期细节；完整会话仍可查看。当前请求过大明确失败，不截断最新用户约束或工具参数。
- MCP 仅支持 stdio tools；Skill 元数据限定 name/description；附件只接受项目内 UTF-8 文本。其余产品范围见 01 文档。
- Token 预算在请求边界检查；缺少服务端计量字段时使用估算值，最终一次响应可能超过阈值。

## 2026-09-16 真实网关兼容性回归

在本机使用 `https://api-gateway.glm.ai` 与 `gpt-5.6-sol` 做了真实连接验证。API Key 仅从环境读取，未写入本目录或测试输出。

最小复现分成两层：

1. 以站点根地址作为 `base_url` 时，CLI 请求 `/models` 得到前端 HTML 404，请求 `/chat/completions` 得到 HTTP 405；改为 `/v1` 后，`/v1/models` 正常返回模型列表，并确认其中包含 `gpt-5.6-sol`。
2. 首次真实 Chat Completions 请求随后返回结构化 HTTP 400：该模型不接受 `max_tokens`，要求使用 `max_completion_tokens`。

Provider 继续默认发送兼容面更广的 `max_tokens`。只有收到状态码 400、`param=max_tokens`、`code=unsupported_parameter`，且消息明确要求 `max_completion_tokens` 时，才切换参数并额外重试一次；协商成功后在当前 Provider 实例中保留选择，后续 Agent 回合不再重复触发已知 400。该兼容重试不按模型名称硬编码，也不改变普通 400、鉴权失败和 429/5xx 的既有处理。

验证结果：

- 新回归测试先稳定复现 400，修复后确认第一次请求使用 `max_tokens`、协商请求只使用 `max_completion_tokens`，且下一次 completion 直接复用已协商字段。
- 真实 `gpt-5.6-sol` 最小只读任务返回正常文本，终态为 `completed`，不再出现 405 或参数错误。
- Provider、Settings 和 CLI 三组相关测试通过；Ruff 与 Mypy 通过。
- 最终全量共收集并通过 **218 项测试**，`git diff --check` 通过。

本机 `~/.zshrc` 的 `AGENT_HARNESS_BASE_URL` 已修正为 `https://api-gateway.glm.ai/v1`。这是机器本地配置，不属于仓库提交。官方 OpenAI 文档页面在验证时被网络侧返回 403，因此参数迁移依据为真实网关的结构化错误契约，并由确定性 HTTP 回归测试锁定。
