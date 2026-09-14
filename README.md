# Coding Agent Harness

一个 Python 编写的本地 Coding Agent CLI：在终端里读取项目、连续对话、修改代码、运行验证并根据错误继续修复。支持任意已有或空项目目录，不需要先启动网页或编写工作区配置。

当前版本 **2.0.0**。交互界面采用简单输入提示和斜杠命令，同时提供单次任务与 JSONL 输出。实现参考 OpenCode 的会话、工具、权限和流式执行设计，具体源码版本与取舍记录在 [devlog](devlog/README.md)。

## 安装与运行

需要 Python 3.11+：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

export AGENT_HARNESS_BASE_URL="https://your-model-service.example/v1"
export AGENT_HARNESS_MODEL="your-model-id"
agent-harness auth login
agent-harness doctor

agent-harness -C /path/to/project
```

模型服务需兼容 OpenAI Chat Completions 的工具调用协议。也可以设置 `AGENT_HARNESS_API_KEY`，不用保存凭据。

```bash
# 单次只读分析
agent-harness -C /path/to/project run --mode plan "解释项目入口与模块关系"

# 编码任务，授权文件修改和固定验证
agent-harness -C /path/to/project run \
  --allow write --allow edit --allow test \
  --test-command "python -m pytest -q" \
  "修复空用户名被接受的问题并通过测试"

# 管道与会话继续
printf '分析代码结构\n' | agent-harness -C /path/to/project run --json
agent-harness -C /path/to/project run --session latest "继续上次任务"
```

在交互模式输入 `/help` 查看命令；`/diff`、`/undo`、`/redo` 管理本轮修改，`/mode plan` 切换只读规划，`/resume ID` 恢复会话。

## 已实现能力

- 连续对话、持久化与跨进程恢复、会话列表/查看/导入/导出。
- 文件读取、Glob、字面量搜索、完整写入、精确编辑、Unified Diff、Shell、固定验证与待办。
- Plan/Build 模式，统一 allow/ask/deny 权限，非交互运行缺少授权时明确退出。
- HTTP/SSE 流式文本与工具参数、用量统计、有限重试、取消、请求/Token/时间预算。
- 按轮 Diff、撤销/重做、外部文件冲突保护；中断后不盲目重放未知结果的工具。
- AGENTS.md、文本附件、上下文压缩、本地 Skills、只读探索子会话、MCP stdio 工具。
- 用户/项目/环境/命令行配置、凭据管理、模型列表和连接诊断。

`completed` 表示当前对话轮正常结束，`verification=passed` 才表示配置的验证命令对当前状态通过。未配置验证时会明确显示 `not_configured`。

CLI 直接操作选定的项目。文件工具会检查路径与变更冲突；获准的 Shell/MCP 是可信本地执行，不是 OS 沙箱，其项目外或系统副作用不保证可撤销。详细限制见[使用手册](devlog/03-user-guide.md)。

## 架构与文档

```text
cli.py（交互 / 脚本 / JSONL）
  └─ coding/session.py（会话循环、恢复、预算与验证）
      ├─ provider.py（模型 HTTP/SSE）
      ├─ tools.py → permissions.py / workspace.py（工具、权限、文件和命令）
      ├─ changes.py（工具变更归属） / storage.py（会话与日志）
      ├─ context.py（指令、附件、压缩）
      └─ extensions.py（Skill、探索子会话、MCP）
```

- [当前架构](devlog/02-architecture.md)
- [完整使用手册](devlog/03-user-guide.md)
- [测试与验收记录](devlog/04-validation.md)
- [开发记录与 OpenCode 源码参考](devlog/README.md)
- [v1 实验台与八轮模型试跑记录](docs/v1-experiment-overview.md)

## 开发与可选网页

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m mypy
python -m ruff check src/agent_harness/coding src/agent_harness/cli.py src/agent_harness/sandbox.py tests/cli
python -m build
```

旧 Streamlit 实验台继续作为可选入口，使用原来的隔离副本和任务状态机：

```bash
python -m pip install -e '.[web]'
streamlit run app.py --server.address localhost
```

旧网页的自主执行依赖 macOS `sandbox-exec`。CLI 的验证使用真实本地 HTTP/SSE 服务、真实文件、Shell/MCP 子进程和 PTY；当前没有真实模型密钥，未将协议测试结果表述为真实模型能力验证。
