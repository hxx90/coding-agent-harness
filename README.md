# Robo

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/release/python-3120/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

<p align="center">
  <img src="distribution/zed/icons/robo.svg" alt="Robo 萝卜机器人 Logo" width="144">
</p>

**用自然语言生成策略，操作可视化机械臂，并独立验证结果。**

Robo 基于 Mistral Vibe，保留代码助手能力，并增加受限 Python 策略、设备租约、
武装、停止 ACK、运行轨迹和录像。当前已验证的机器人路径是 **Mac 上的
MuJoCo / Robosuite Panda 仿真**；这不代表已完成真实机械臂验收。

本 README 对应 `hxx90/coding-agent-harness` 的 **`codex/robo-hardware-harness`**
分支，实现位于 `vibe/`。同仓库 `main` 的 `src/agent_harness/` 是另一套实现；
旧 RSI 的 benchmark-protocol 工程也不是本 CLI。不要按目录名判断版本。

### 当前能力与验证边界

| 能力 | 当前状态 |
| --- | --- |
| Lift：抓起红方块 | 自然语言 → 自主策略 → 独立验证 → 停止 ACK → 释放租约已跑通 |
| Stack：将红方块放到绿方块上 | 可选场景，已有一次自主全链路成功；默认仍为 Lift |
| 视觉输入 | RGB-D、公开相机标定、颜色分割点云；需显式启用 `rgbd` |
| RSI H1 几何 helper | 固定哈希原件，可选、默认关闭；迁移后的增益尚未证明，Stack 要求关闭 |
| 截断与动作控制 | 派发前完整性检查；固定目标逐周期反馈 `move_to`；自动重新生成默认关闭 |

截至 2026-11-19，已提交基线 `5dcb86e` 对应的本地记录为 Lift 固定场景
907 / 911 / 919 共 3/3 自主全链路成功，Stack 场景 1201 为 1/1。
两组均关闭 H1 和自动生成恢复，未设置客户端 token 配额。Lift 907 在同一会话内
生成了第二段策略才成功。**这些是小样本工程验收，不是总体成功率或官方
RoboWorld / CaP-X 成绩。** 原始录像和实验报告保存在实验工作区，未随仓库分发。

### 从本分支安装

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/getting-started/installation/)
0.8.0+ 和支持 Textual 的终端。可视化路径已在 Apple Silicon Mac 验证；其他平台
尚无同等验收记录。

```bash
# 仅首次获取；已有工作目录时直接进入原目录，不要覆盖或重置。
git clone --branch codex/robo-hardware-harness \
  https://github.com/hxx90/coding-agent-harness.git robo
cd robo
uv sync --extra simulator
uv run --no-sync robo --help
```

上游 `mistral-vibe` 安装包和 Mistral 一键安装脚本不包含本分支的 Robo 改动。
本地尚未推送的提交也不会出现在新 clone 中；用 `git log -1 --oneline` 确认版本。
已安装仿真依赖后使用 `uv run --no-sync` 启动，避免普通同步移除可选仿真依赖。

## Table of Contents

- [当前能力与验证边界](#当前能力与验证边界)
- [从本分支安装](#从本分支安装)
- [Mac 可视化快速开始](#quick-start)
- [场景、H1 与运行记录](#场景h1-与运行记录)
- [常见问题与开发验证](#常见问题与开发验证)
- [Features](#features)
  - [Built-in Agents](#built-in-agents)
  - [Subagents and Task Delegation](#subagents-and-task-delegation)
  - [Interactive User Questions](#interactive-user-questions)
- [Terminal Requirements](#terminal-requirements)
- [Quick Start](#quick-start)
- [Usage](#usage)
  - [Interactive Mode](#interactive-mode)
  - [Trust Folder System](#trust-folder-system)
  - [Programmatic Mode](#programmatic-mode)
  - [Hardware Harness and CaP-X](#hardware-harness-and-cap-x)
- [Voice Mode](#voice-mode)
- [Slash Commands](#slash-commands)
  - [Built-in Slash Commands](#built-in-slash-commands)
  - [Custom Slash Commands via Skills](#custom-slash-commands-via-skills)
- [Skills System](#skills-system)
  - [Creating Skills](#creating-skills)
  - [Skill Discovery](#skill-discovery)
  - [Managing Skills](#managing-skills)
- [Configuration](#configuration)
  - [Configuration File Location](#configuration-file-location)
  - [API Key Configuration](#api-key-configuration)
  - [OpenTelemetry Tracing](#opentelemetry-tracing)
  - [Custom System Prompts](#custom-system-prompts)
  - [Custom Agent Configurations](#custom-agent-configurations)
  - [Tool Management](#tool-management)
  - [MCP Server Configuration](#mcp-server-configuration)
  - [Session Management](#session-management)
  - [Update Settings](#update-settings)
  - [Custom Vibe Home Directory](#custom-vibe-home-directory)
- [Editors/IDEs](#editorsides)
- [Resources](#resources)
- [Data collection & usage](#data-collection--usage)
- [License](#license)

## Features

- **Interactive Chat**: A conversational AI agent that understands your requests and breaks down complex tasks.
- **Powerful Toolset**: A suite of tools for file manipulation, code searching, version control, and command execution, right from the chat prompt.
  - Read, write, and patch files (`read`, `write_file`, `edit`).
  - Execute shell commands, with managed shell sessions, polling, and stdin helpers available during rollout.
  - Recursively search code with `grep` (with `ripgrep` support).
  - Manage a `todo` list to track the agent's work.
  - Ask interactive questions to gather user input (`ask_user_question`).
  - Delegate tasks to subagents for parallel work (`task`).
- **Project-Aware Context**: Vibe automatically scans your project's file structure and Git status to provide relevant context to the agent, improving its understanding of your codebase.
- **Advanced CLI Experience**: Built with modern libraries for a smooth and efficient workflow.
  - Autocompletion for slash commands (`/`) and file paths (`@`).
  - Image attachments via `@` mentions — `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` files are sent to vision-capable models (e.g. Mistral Medium 3.5) as native multimodal content.
  - Persistent command history.
  - Beautiful Themes.
- **Highly Configurable**: Customize models, providers, tool permissions, and UI preferences through a simple `config.toml` file.
- **Safety First**: Features tool execution approval.
- **Hardware Harness**: Discovers simulated or MCP-backed devices and routes motion through leases, approval, observation, acknowledged stop, and per-run traces.
- **Multiple Built-in Agents**: Choose from different agent profiles tailored for specific workflows.

### Built-in Agents

Vibe comes with several built-in agent profiles, each designed for different use cases:

- **`ask`**: Requires approval for tool executions.
- **`plan`**: Read-only agent for exploration and planning. Auto-approves safe tools like `grep` and `read`.
- **`accept-edits`**: The default agent. Auto-approves file edits only (`write_file`, `edit`). Useful for code refactoring.
- **`auto-approve`**: Auto-approves all tool executions. Use with caution.

Use the `--agent` flag to select a different agent:

```bash
vibe --agent plan
```

To change the default agent used when `--agent` is not passed, set
`default_agent` in your `config.toml`:

```toml
default_agent = "plan"
```

Valid values are `ask`, `plan`, `accept-edits`, `auto-approve`,
`lean` (only when listed in `installed_agents`), or the name of any
custom agent file in `~/.vibe/agents/` or the project's `.vibe/agents/`
directory. Subagents such as `explore` are not accepted.

> Note: `default_agent` applies in both interactive and programmatic
> (`-p` / `--prompt`) sessions. Pass `--auto-approve` or `--yolo` with any
> agent when a run should approve all tool calls without prompting.

### Subagents and Task Delegation

Vibe supports subagents for delegating tasks. Subagents run independently and can perform specialized work without user interaction, preventing the context from being overloaded.

The `task` tool allows the agent to delegate work to subagents:

```
> Can you explore the codebase structure while I work on something else?

🤖 I'll use the task tool to delegate this to the explore subagent.

> task(task="Analyze the project structure and architecture", agent="explore")
```

Create custom subagents by adding `agent_type = "subagent"` to your agent configuration. Vibe comes with a built-in subagent called `explore`, a read-only subagent for codebase exploration and skill loading used internally for delegation.

### Interactive User Questions

The `ask_user_question` tool allows the agent to ask you clarifying questions during its work. This enables more interactive and collaborative workflows.

```
> Can you help me refactor this function?

🤖 I need to understand your requirements better before proceeding.

> ask_user_question(questions=[{
    "question": "What's the main goal of this refactoring?",
    "options": [
        {"label": "Performance", "description": "Make it run faster"},
        {"label": "Readability", "description": "Make it easier to understand"},
        {"label": "Maintainability", "description": "Make it easier to modify"}
    ]
}])
```

The agent can ask multiple questions at once, displayed as tabs. Each question supports 2-4 options plus an automatic "Other" option for free text responses.

## Terminal Requirements

Vibe's interactive interface requires a modern terminal emulator. Recommended terminal emulators include:

- **WezTerm** (cross-platform)
- **Alacritty** (cross-platform)
- **Ghostty** (Linux and macOS)
- **Kitty** (Linux and macOS)

Most modern terminals should work, but older or minimal terminal emulators may have display issues.

## Quick Start

以下步骤使用 Mac 可视化 Lift，H1 关闭。模型通过自然语言自行生成策略。

1. **在仓库根目录确认环境。** 首次安装执行上一节的 `uv sync --extra simulator`。
   已有安装可直接使用原工作目录，例如 `mistral-vibe`；不必改目录名。

2. **保留已有模型配置。** 模型和 provider 可配置在 `~/.vibe/config.toml`，
   凭据使用该 provider 指定的环境变量或 `~/.vibe/.env`，不要提交进 Git。
   默认 Mistral provider 可用 `uv run --no-sync robo --setup` 配置；已有自定义
   provider 不要为了跑仿真而换模型。后文的 Mistral 密钥说明仅适用于该 provider。

3. **配置仿真 MCP。** 没有项目配置时：

   ```bash
   mkdir -p .vibe
   # 只在文件不存在时创建，保留已有项目配置。
   if [ ! -e .vibe/config.toml ]; then
     cp examples/robo/lift-rgbd-off.toml .vibe/config.toml
   fi
   ```

   文件已经存在时，手动合并 [Lift 示例](examples/robo/lift-rgbd-off.toml) 中的
   `robo-mujoco` MCP 配置，保留模型及其他设置；不要复制覆盖或重复定义同名 server。
   示例关闭原始 MCP 动作工具，让模型通过带租约和武装检查的 `robo_*` 工具执行动作。

   在文件开头、所有 `[表]` 之前设置顶层 `enable_update_checks = false`，避免收到
   上游包升级提示。在同一项目文件中添加或更新以下独立表，**不要放进
   `mcp_servers.env`**：

   ```toml
   [generation]
   enabled = true
   limit_tokens = false
   recover_once = false

   [session_logging]
   generate_titles = false
   ```

   这会保留完整性检查与安全收尾，并关闭客户端 token 配额及自动重新生成。
   请求/时间/动作额度仍生效（默认 16 请求、480 秒、80 动作），策略还有自身的
   AST 与执行限制。服务端仍有模型上下文和输出限制。完整说明见
   [生成完整性与会话控制](docs/robo-cli-generation-recovery.md)。

4. **启动真实 CLI。**

   ```bash
   uv run --no-sync robo --legacy-harness --workdir .
   ```

   输入以下自然语言任务，并按 CLI 提示批准硬件工具：

   > 仅操作 mujoco-panda-1 仿真。读取设备 Manifest，连接、获取租约并武装。
   > 根据当前 RGB-D 视觉观测生成受限 Python 策略，抓起红色方块；
   > 策略内验证 lift_object，再在策略外独立验证。最后停止，确认 ACK 并释放租约。
   > 报告任务验证结果与安全收尾结果，失败时说明停在哪一步。

5. **查看画面与结果。** 连接设备后会启动独立 Viewer，策略卡片显示源码、回执、
   验证和证据路径。需要停止时按 `Ctrl+Shift+X`，检查返回的停止 ACK。
   **关闭 Viewer 只关闭画面，不停止控制。** 正常结束应完成停止和释放租约。

## 场景、H1 与运行记录

退出当前 CLI 后，编辑 `.vibe/config.toml` 中 **`robo-mujoco` 对应的
`[mcp_servers.env]`**，再重新启动。仅在父 shell 中 export 不足以保证变量传入
MCP 子进程。

| 设置 | 用途 |
| --- | --- |
| `ROBO_SIM_TASK = "Lift"` / `"Stack"` | 选择任务；省略时为 Lift |
| `ROBO_SIM_OBSERVATION_MODE = "rgbd"` | 相机观测；省略时原适配器默认 `privileged`，含物体真值，不能当作视觉评测 |
| `ROBO_SIM_GEOMETRY_LIBRARY = "off"` / `"h1-v8-v9"` | 关闭或启用 H1；H1 仅用于 RGB-D Lift，Stack 保持 off |
| `ROBO_SIM_SEED = "1201"` | 固定初始场景；省略时随机，不保证模型输出可复现 |
| `ROBO_SIM_HEADLESS = "1"` | 不打开 Viewer，仍可生成证据和录像 |

Stack 使用 [Stack 配置示例](examples/robo/stack-rgbd-off.toml)，任务可表述为：

> 仅操作 mujoco-panda-1 仿真。连接、获取租约并武装，根据 RGB-D 观测把红方块
> 放到绿方块上，松爪并撤离。策略内和策略外分别验证 stack_objects，
> 最后停止、确认 ACK 并释放租约，分别报告任务与收尾结果。

Stack 验证还要求连续 20 个控制周期稳定；`verify()` 本身不推进物理时间。
见 [Stack 验证契约](docs/robo-stack-pilot.md)。

固定目标移动使用 `move_to`；原 `move_cartesian` 仍是重复归一化增量。
回执中的到位状态在 `receipt["result"]["motion"]`，包含误差、周期数与退出原因。
**到位不等于抓取或堆叠成功**，以独立任务验证为准；兜底收尾也不能计为模型自主完成。
详见 [动作回执与反馈控制](docs/robo-target-motion.md)。

默认记录位置如下；使用工具返回的 URI 可以定位本次实际文件：

| 内容 | 默认位置 |
| --- | --- |
| Runtime 事件轨迹 | `<会话目录>/hardware-runs/<run_id>.jsonl` |
| 策略源码与执行结果 | `<会话目录>/hardware-runs/policies/<policy_id>.py` / `.json` |
| MP4 录像 | `<会话目录>/hardware-runs/recordings/robo-mujoco/` |
| RGB / RGB-D 与控制周期证据 | `<会话目录>/hardware-runs/evidence/robo-mujoco/` |

停止会将录像落盘，随后 `robo_observe` 的 `snapshot.state.recording_uri` 提供路径。
关闭会话日志时，记录根目录为 `<项目>/.vibe/hardware-runs/<session_id>/`。
若需要完整模型请求/响应审计，在 `[generation]` 中设置 `audit_dir` 为一个新的
本地绝对路径：生成日志写入该目录的 `generation.jsonl`，Runtime 轨迹、证据和录像
改写到该目录的 `hardware-runs/`；策略文件仍按工具返回的源码 URI 查找。
审计含任务和模型上下文，默认保留在本地，不提交 Git。

## 常见问题与开发验证

| 现象 | 先检查 |
| --- | --- |
| 只有 `sim-arm-1`，没有 Panda | 检查项目配置是否受信任、`robo-mujoco` 是否启动；`sim-arm-1` 是协议测试设备，不是物理仿真 |
| 提示缺少 robosuite / mujoco | 在仓库执行 `uv sync --extra simulator`，之后用 `uv run --no-sync` 启动 |
| 有控制但没有窗口 | 检查 MCP 环境中的 `ROBO_SIM_HEADLESS`；Viewer 可独立关闭，画面缺失不等于设备停止 |
| `motion` 字段错误或移动未到位 | 按完整 CommandReceipt 读取 `result.motion`；检查 `reason` 和误差，不把失败回执当作完成 |
| 模型输出截断 | 保留完整性防护；确认 `limit_tokens=false`，没有显式 `--max-tokens` 或其他调用方 cap；再查 finish reason，服务端限制仍可能截断 |

在仓库根目录运行相关检查（不调用模型）：

```bash
uv run --no-sync pytest tests/core/hardware tests/core/tools/builtins/test_robo.py \
  tests/core/tools/builtins/test_robo_policy_contract.py \
  tests/core/agent_loop/test_generation_guard.py tests/hardware_sim \
  tests/app_server/test_tool_projection.py
uv run --no-sync ruff check .
uv run --no-sync pyright
```

更多资料：[本地运行手册](docs/robo-harness-local-runbook.md)、
[RGB-D / H1 原件与坐标契约](docs/robo-h1-lift-runbook.md)、[贡献约束](AGENTS.md)。
旧 H1 手册中的单次 backend smoke 和历史复放不等同于真实 CLI 自主验收；
旧实验的 token 配额也不是本页推荐的交互配置。

以下为继承自 Vibe 的通用 CLI 参考。源码安装同时提供 `robo` 和 `vibe` 别名；
在本分支工作时可将示例中的 `vibe` 命令写成 `uv run --no-sync robo`。

## Usage

### Interactive Mode

Simply run `vibe` to enter the interactive chat loop.

- **Multi-line Input**: Press `Ctrl+J` or `Shift+Enter` for select terminals to insert a newline.
- **File Paths**: Reference files in your prompt using the `@` symbol for smart autocompletion (e.g., `> Read the file @src/agent.py`). A bare `@` quickly lists immediate non-hidden entries; after a path character, Git workspaces suggest tracked and non-ignored files. Pasting a standalone existing absolute or home-relative file or folder also creates a mention.
- **Shell Commands**: Prefix any command with `!` to execute it directly in your shell, bypassing the agent (e.g., `> !ls -l`).
- **External Editor**: Press `Ctrl+G` to edit your current input in an external editor.
- **Tool Output Toggle**: Press `Ctrl+O` to toggle the tool output view.
- **Todo View Toggle**: Press `Ctrl+T` to toggle the todo list view.
- **Debug Console**: Press `Ctrl+\` to toggle the debug console.
- **Agent Selection**: Press `Shift+Tab` to cycle through agents (ask, plan, ...).
- **Queueing**: Prompts submitted while the agent is working are queued by the app server. Empty `Enter` or `Ctrl+Enter` steers the queued prompts into the active turn; on Unified Harness sessions, this atomically consumes the stored queue item. `Ctrl+C` removes the newest queued prompt. `Escape` interrupts the active turn and pauses remaining prompts, and `Enter` resumes a paused queue. Shell commands and non-side-channel slash commands require an idle session.
- **Exit**: Type `/exit`, `exit`, `quit`, `:q`, or `:quit` in the input box, or press `Ctrl+C` / `Ctrl+D` twice within ~1 second. Set `ask_confirmation_on_exit = false` (or toggle it in `/config`) to make `Ctrl+D` quit on the first press; `Ctrl+C` always requires confirmation.

### Copying & Text Selection

- **Copy**: Use `Ctrl+Y` or `Ctrl+Shift+C` to copy the current selection to clipboard. With autocopy enabled (default via `autocopy_to_clipboard = true`), mouse selection automatically copies on release and shows a brief confirmation.
- **Multi-click selection**: Double-click selects a word, triple-click selects the paragraph. Dragging extends the selection at the same granularity.

You can start Vibe with a prompt using the following command:

```bash
vibe "Refactor the main function in cli/main.py to be more modular."
```

### Trust Folder System

Vibe includes a trust folder system to ensure you only run the agent in directories you trust. When you first run Vibe in a new directory which contains a `.vibe` subfolder, it may ask you to confirm whether you trust the folder.

Trusted folders are remembered for future sessions. You can manage trusted folders through its configuration file `~/.vibe/trusted_folders.toml`.

This safety feature helps prevent accidental execution in sensitive directories.

### Programmatic Mode

You can run Vibe non-interactively by piping input or using the `--prompt` flag. This is useful for scripting.

```bash
vibe --prompt "Refactor the main function in cli/main.py to be more modular."
```

By default, it uses your configured `default_agent` (`accept-edits` unless changed).
To approve all tool calls without prompting, pass `--auto-approve` or `--yolo`
(also available for interactive sessions):

```bash
vibe --prompt "Refactor the main function in cli/main.py to be more modular." --auto-approve
```

#### Programmatic Mode Options

When using `--prompt`, you can specify additional options:

- **`--max-turns N`**: Limit the maximum number of assistant turns. The session will stop after N turns.
- **`--max-price DOLLARS`**: Set a maximum cost limit in dollars. The session will be interrupted if the cost exceeds this limit.
- **`--max-tokens N`**: Set a maximum cumulative LLM token budget for the session, counting both prompt and completion tokens. The session will be interrupted if usage exceeds this limit.
- **`--agent NAME`**: Select the agent profile for this run.
- **`--auto-approve`, `--yolo`**: Approves all tool calls without prompting, including in interactive sessions. Can be combined with any `--agent` value.
- **`--enabled-tools TOOL`**: Enable specific tools. In programmatic mode, this disables all other tools. Can be specified multiple times. Supports exact names, glob patterns (e.g., `bash*`), or regex with `re:` prefix (e.g., `re:^serena_.*$`).
- **`--disabled-tools TOOL`**: Disable specific tools after `--enabled-tools` filtering. Can be specified multiple times. Supports exact names, glob patterns (e.g., `bash*`), or regex with `re:` prefix (e.g., `re:^serena_.*$`).
- **`--output FORMAT`**: Set the output format. Options:
  - `text` (default): Human-readable text output
  - `json`: All messages as JSON at the end
  - `streaming`: Newline-delimited JSON per message

Example:

```bash
vibe --prompt "Analyze the codebase" --max-turns 5 --max-price 1.0 --max-tokens 50000 --output json
```

### Hardware Harness and CaP-X

Robo includes a deterministic local arm (`sim-arm-1`) and the model-facing
`robo_devices`, `robo_observe`, `robo_run_policy`, `robo_verify`, and `robo_stop`
tools. Task-changing actions are expressed as bounded Python policies; the legacy
single-action and sequence implementations remain internal compatibility paths and are
not exposed to the model. Start the
CLI with `uv run --no-sync robo --legacy-harness --workdir .` and ask it to discover, connect, lease, explicitly
arm, and operate the simulated arm.

For a visible Franka Panda simulation on macOS, install the optional simulator
extra with `uv sync --extra simulator`, configure the bundled `robo-sim-mcp`
adapter, and connect `mujoco-panda-1`. Robo launches an independent, display-only
native MuJoCo Viewer process fed by a local mmap state mirror. Its resizable window
supports native mouse orbit, pan, and scroll zoom; closing the Viewer does not stop the
simulator MCP. Robo also captures upright visual evidence and
records an MP4 under the session's
`hardware-runs/recordings/` directory. Hardware tool cards expose state,
faults, recent trace events, evidence URIs, command receipts, and verification
checks. Code-policy cards also expose the exact source, artifact URI, and ordered
observation/action/verification outputs. Policy source is stored under the session's
`hardware-runs/policies/` directory. In the default Legacy Harness, `Ctrl+Shift+X` requests a global
acknowledged stop before interrupting the active agent turn.

After stopping, release the old lease before reconnecting. Releasing the same
lease is idempotent; a recovered connection must acquire a fresh lease and be
armed again. The local runbook contains the complete configuration and prompt.

To expose Robo as a CaP-X coding agent:

```bash
uv run robo-capx-bridge -C . --host 127.0.0.1 --port 8110 --trust
uv run robo-capx-smoke --url http://127.0.0.1:8110/chat/completions
```

See the [Chinese Code-as-Policies plan](docs/robo-code-as-policy-plan.md) and
[local runbook](docs/robo-harness-local-runbook.md) for MCP
adapter configuration, trace locations, Mac limitations, and the Linux/NVIDIA
CaP-X command.

## Voice Mode

> [!WARNING]
> Voice mode is experimental and may change in future releases.

Voice mode allows you to dictate input using your microphone instead of typing.

### Activating Voice Mode

Toggle voice mode on or off with the `/voice` slash command:

```
> /voice
```

### Recording Shortcuts

| Shortcut | Action           |
| -------- | ---------------- |
| `Ctrl+R` | Start recording  |
| Any key  | Stop recording   |
| `Escape` | Cancel recording |
| `Ctrl+C` | Cancel recording |

## Slash Commands

Use slash commands for meta-actions and configuration changes during a session.

### Built-in Slash Commands

Vibe provides several built-in slash commands. Use slash commands by typing them in the input box:

```
> /help
```

If a model response is interrupted by a backend error, use `/retry` to continue
from the partial response. Add optional guidance after the command, for example
`/retry keep the conclusion concise`.

Use `/mcp` or `/connectors` to browse configured MCP servers and workspace
connectors. The browser starts on the first item; press Up or Left to focus its
fuzzy search bar, then Up again to wrap to the last item.

### Custom Slash Commands via Skills

You can define your own slash commands through the skills system. Skills are reusable components that extend Vibe's functionality.

To create a custom slash command:

1. Create a skill directory with a `SKILL.md` file
2. Set `user-invocable = true` in the skill metadata
3. Define the command logic in your skill

Example skill metadata:

```markdown
---
name: my-skill
description: My custom skill with slash commands
user-invocable: true
---
```

Custom slash commands appear in the autocompletion menu alongside built-in commands.

## Skills System

Vibe's skills system allows you to extend functionality through reusable components. Skills can add new tools, slash commands, and specialized behaviors.

Vibe follows the [Agent Skills specification](https://agentskills.io/specification) for skill format and structure.

### Creating Skills

Skills are defined in directories with a `SKILL.md` file containing metadata in YAML frontmatter. For example, `~/.vibe/skills/code-review/SKILL.md`:

```markdown
---
name: code-review
description: Perform automated code reviews
license: MIT
compatibility: Python 3.12+
user-invocable: true
allowed-tools:
  - read
  - grep
  - ask_user_question
---

# Code Review Skill

This skill helps analyze code quality and suggest improvements.
```

### Skill Discovery

Vibe discovers skills from multiple locations:

1. **Custom paths**: Configured in `config.toml` via `skill_paths`
2. **Standard Agent Skills path** (project root, trusted folders only): `.agents/skills/` — [Agent Skills](https://agentskills.io) standard
3. **Local project skills** (project root, trusted folders only): `.vibe/skills/` in your project
4. **Global skills directories**: `~/.vibe/skills/` and `~/.agents/skills/`

```toml
skill_paths = ["/path/to/custom/skills"]
```

### Managing Skills

Enable or disable skills using patterns in your configuration:

```toml
# Enable specific skills
enabled_skills = ["code-review", "test-*"]

# Disable specific skills
disabled_skills = ["experimental-*"]
```

Skills support the same pattern matching as tools (exact names, glob patterns, and regex).

## Configuration

### Configuration File Location

Vibe is configured via a `config.toml` file. It looks for this file first in `./.vibe/config.toml` and then falls back to `~/.vibe/config.toml`.

### Theme

The default `auto` theme follows the terminal background when it can be detected, then the operating-system light/dark preference. Choose another theme with `/theme` or set it explicitly:

```toml
theme = "dracula"
```

### API Key Configuration

For the default Mistral provider, you need a Mistral API key from
[https://console.mistral.ai](https://console.mistral.ai). Custom providers use their
own configured credentials; a Mistral key is not required for every Robo setup.

You can configure your API key using `vibe --setup`, or through one of the methods below.

Vibe supports multiple ways to configure your API keys:

1. **Interactive Setup (Recommended for first-time users)**: When you run Vibe for the first time or if your API key is missing, Vibe will prompt you to enter it. The key will be securely saved to `~/.vibe/.env` for future sessions.

2. **Environment Variables**: Set your API key as an environment variable:

   ```bash
   export MISTRAL_API_KEY="your_mistral_api_key"
   ```

3. **`.env` File**: Create a `.env` file in `~/.vibe/` and add your API keys:

   ```bash
   MISTRAL_API_KEY=your_mistral_api_key
   ```

   Vibe automatically loads API keys from `~/.vibe/.env` on startup. Environment variables take precedence over the `.env` file if both are set.

**Note**: The `.env` file is specifically for API keys and other provider credentials. General Vibe configuration should be done in `config.toml`.

### Custom Domains

If you use a Mistral-compatible deployment instead of the default `console.mistral.ai` / `api.mistral.ai`, you can point browser sign-in at it. The credential is still a Mistral API key.

Run `vibe --setup`, choose **Launch browser** then **Other**, enter your login domain, and sign in through the browser. A bare domain is prefixed with `https://`, and the auth API base is derived as `DOMAIN/api`. The overridden `mistral` provider is saved to your user config so subsequent runs reuse it.

**Note**: the wizard reads any custom `browser_auth_base_url` already set in `config.toml`. Choosing **Other** pre-fills that configured domain so you can confirm or edit it. Choosing **Mistral AI** while a custom domain is configured warns you first — press **Enter** again to confirm the reset to the default domain, which is then persisted.

### TLS and Corporate Certificate Authorities

By default, Vibe uses the bundled `certifi` certificate roots for outbound HTTPS requests. If your organization installs private certificate authorities in the operating system trust store, you can opt in to the system trust store in `config.toml`:

```toml
enable_system_trust_store = true
```

`SSL_CERT_FILE` and `SSL_CERT_DIR` are still supported and are loaded as additional trust anchors.

### OpenTelemetry Tracing

Vibe can export traces for agent, model, and tool operations over OTLP/HTTP. Enable tracing in `config.toml`:

```toml
enable_otel = true
```

By default, Vibe sends traces to the telemetry endpoint associated with the configured Mistral provider and authenticates with that provider's API key. `enable_telemetry` must also remain enabled.

To send traces to another collector, configure its base URL. Vibe appends `/v1/traces`; configure authentication with the standard `OTEL_EXPORTER_OTLP_*` environment variables when needed.

```toml
enable_otel = true
otel_endpoint = "https://collector.example.com:4318"
```

Span attributes are redacted on the client before export. The default mode redacts sensitive values, `strict` redacts sensitive attributes entirely, and `none` disables redaction:

```toml
otel_redaction = "default" # "default", "strict", or "none"
```

Use `none` only when the collector is trusted to receive potentially sensitive prompt, response, and tool data.

### Custom System Prompts

You can create `AGENTS.md` files to add custom instructions. You can also replace the entire system prompt.

Place `AGENTS.md` files in:
- `~/.vibe/AGENTS.md` — user-level instructions for all projects
- Project directories — project-specific instructions, loaded from cwd up to the trust root

Priority: closer directories override more distant ones. Instructions in `AGENTS.md` override the default system prompt. Files are only loaded for trusted folders.

Custom system prompts entirely replace the default one (`prompts/cli.md`). Create a markdown file in the `~/.vibe/prompts/` directory with your custom prompt content.

To use a custom system prompt, set the `system_prompt_id` in your configuration to match the filename (without the `.md` extension):

```toml
# Use a custom system prompt
system_prompt_id = "my_custom_prompt"
```

This will load the prompt from `~/.vibe/prompts/my_custom_prompt.md`.

Project-local prompts in `.vibe/prompts/` are also supported and override user-level prompts with the same name. This applies to all custom prompts (system and compaction).

### Custom Compaction Prompts

Compaction uses the built-in prompt at `prompts/compact.md` by default. You can replace it with a custom prompt from `~/.vibe/prompts/` (or `.vibe/prompts/`) using the same resolution rules as system prompts.

To use a custom compaction prompt, set `compaction_prompt_id` in your configuration to match the filename (without the `.md` extension):

```toml
# Use a custom compaction prompt
compaction_prompt_id = "my_compaction_prompt"
```

Any extra instructions passed to `/compact ...` are appended after the configured compaction prompt.

Compaction keeps the same session and visible conversation. Later model requests
use the latest compacted context followed by newer messages.

### Custom Agent Configurations

You can create custom agent configurations for specific use cases (e.g., red-teaming, specialized tasks) by adding agent-specific TOML files in the `~/.vibe/agents/` directory.

To use a custom agent, run Vibe with the `--agent` flag:

```bash
vibe --agent my_custom_agent
```

Vibe will look for a file named `my_custom_agent.toml` in the agents directory and apply its configuration.

Example custom agent configuration (`~/.vibe/agents/redteam.toml`):

```toml
# Custom agent configuration for red-teaming
active_model = "mistral-medium-3.5"
system_prompt_id = "redteam"

# Disable some tools for this agent
disabled_tools = ["edit", "write_file"]

# Override tool permissions for this agent
[tools.bash]
permission = "always"

[tools.read]
permission = "always"
```

Note: This implies that you have set up a redteam prompt named `~/.vibe/prompts/redteam.md`.

### Tool Management

The built-in shell surface is controlled by the `managed_shell_tools_enabled` config
field and the `vibe_cli_managed_shell_tools` GrowthBook experiment. The default variant
keeps the legacy one-shot `bash` tool, including its existing Windows behavior.
The managed variant exposes OS-native shell tools:
POSIX systems, including WSL where Vibe runs as Linux, get managed `bash`,
`bash_output`, `bash_stdin`, `bash_sessions`, and `bash_log_file`; native Windows
gets `git_bash`, `git_bash_output`, `git_bash_stdin`, `git_bash_sessions`, and
`git_bash_log_file` when Git Bash is available. If Git Bash is unavailable,
native Windows falls back to `powershell`, `powershell_output`,
`powershell_stdin`, `powershell_sessions`, and `powershell_log_file`.

Managed shell sessions return a `session_id`, inline output, a cursor for polling
more output, and a log path under `~/.vibe/shell-tool/sessions/`. Long-running
commands can be left alive with `background = true`, and interactive commands can
be driven with the matching stdin tool.

POSIX `bash` reads permissions, allowlists, and denylists from `[tools.bash]`.
Native Windows `git_bash` reads them from `[tools.git_bash]`; native Windows
`powershell` reads them from `[tools.powershell]`. Neither Windows tool reads
`[tools.bash]`. Git Bash is preferred when Vibe can resolve a usable `bash.exe`
from PATH, Git for Windows, or standard Git install locations. If Git Bash is
unavailable, the PowerShell resolution order is `pwsh.exe`, then
`powershell.exe`. `cmd.exe` is not used by the managed Windows shell tools.

```toml
[tools.git_bash]
permission = "ask"
shell = "C:\\Program Files\\Git\\bin\\bash.exe"

[tools.powershell]
permission = "ask"
shell = "powershell.exe"
```

The rollout assignment is server-managed and is not a `config.toml` option.

#### Enable/Disable Tools with Patterns

You can control which tools are active using `enabled_tools` and `disabled_tools`.
These fields support exact names, glob patterns, and regular expressions.
When both are set, `enabled_tools` first narrows the tool set, then
`disabled_tools` removes matching tools from that set.

Examples:

```toml
# Only enable tools that start with "serena_" (glob)
enabled_tools = ["serena_*"]

# Regex (prefix with re:) — matches full tool name (case-insensitive)
enabled_tools = ["re:^serena_.*$"]

# Disable a group with glob; everything else stays enabled
disabled_tools = ["mcp_*", "grep"]
```

Notes:

- MCP tool names use underscores, e.g., `serena_list` not `serena.list`.
- Regex patterns are matched against the full tool name using fullmatch.

### MCP Server Configuration

You can configure MCP (Model Context Protocol) servers to extend Vibe's capabilities. Add MCP server configurations under the `mcp_servers` section:

Remote MCP servers can be added non-interactively from the shell. Static auth
is selected when `--api-key-env` or `--header` is provided; otherwise the
server uses OAuth and starts browser login by default.

```bash
vibe mcp add mistralai \
  --url https://api.mistral.ai/mcp \
  --transport streamable-http \
  --api-key-env MISTRAL_API_KEY

vibe mcp add linear \
  --url https://mcp.linear.app/mcp

vibe mcp remove mistralai
```

Use `--no-login` to persist an OAuth server without starting login. Static auth
also supports repeatable `--header`, `--api-key-header`, `--api-key-format`,
`--startup-timeout-sec`, and `--tool-timeout-sec`. Run `vibe mcp add --help`
for the complete command reference. `vibe mcp remove <name>` removes the server
from the user configuration. Removing an OAuth server also deletes its stored
tokens, client information, and configuration fingerprint when available.

Hosted OAuth MCP servers can also be added from inside Vibe:

```text
/mcp add https://mcp.linear.app/mcp
/mcp add https://mcp.example.com/mcp --name docs --scope read --transport http --no-login
```

`/mcp add` is OAuth-only. It writes `auth.type = "oauth"` with optional
scopes and starts login by default. It uses `transport = "streamable-http"`
unless you pass `--transport http`. Pass `--no-login` to add the server without
starting OAuth login. The shortcut supports `streamable-http` and `http`
transports.

```toml
# Example MCP server configurations
[[mcp_servers]]
name = "my_http_server"
transport = "http"
url = "http://localhost:8000"

[mcp_servers.auth]
type = "static"
headers = { "X-Client" = "vibe" }
api_key_env = "MY_API_KEY_ENV_VAR"
api_key_header = "Authorization"
api_key_format = "Bearer {token}"

[[mcp_servers]]
name = "my_streamable_server"
transport = "streamable-http"
url = "http://localhost:8001"

[mcp_servers.auth]
type = "static"
headers = { "X-Client" = "vibe" }

[[mcp_servers]]
name = "fetch_server"
transport = "stdio"
command = "uvx"
args = ["mcp-server-fetch"]
env = { "DEBUG" = "1", "LOG_LEVEL" = "info" }
```

Supported transports:

- `http`: Standard HTTP transport
- `streamable-http`: HTTP transport with streaming support
- `stdio`: Standard input/output transport (for local processes)

Key fields:

- `name`: A short alias for the server (used in tool names)
- `transport`: The transport type
- `url`: Base URL for HTTP transports
- `headers`: Additional HTTP headers
- `api_key_env`: Environment variable containing the API key
- `command`: Command to run for stdio transport
- `args`: Additional arguments for stdio transport
- `startup_timeout_sec`: Timeout in seconds for the server to start and initialize (default 10s)
- `tool_timeout_sec`: Timeout in seconds for tool execution (default 60s)
- `env`: Environment variables to set for the MCP server of transport type stdio

HTTP MCP servers can use either static auth or OAuth. Both use an `auth` block;
legacy top-level `api_key_env` / `headers` keys are still accepted and promoted
to static auth when Vibe loads the configuration.

```toml
[[mcp_servers]]
name = "linear"
transport = "streamable-http"
url = "https://mcp.linear.app/mcp"

[mcp_servers.auth]
type = "oauth"
scopes = []
```

MCP tools are named using the pattern `{server_name}_{tool_name}` and can be configured with permissions like built-in tools:

```toml
# Configure permissions for specific MCP tools
[tools.fetch_server_get]
permission = "always"

[tools.my_http_server_query]
permission = "ask"
```

MCP server configurations support additional features:

- **Environment variables**: Set environment variables for MCP servers
- **Custom timeouts**: Configure startup and tool execution timeouts

Example with environment variables and timeouts:

```toml
[[mcp_servers]]
name = "my_server"
transport = "http"
url = "http://localhost:8000"
env = { "DEBUG" = "1", "LOG_LEVEL" = "info" }
startup_timeout_sec = 15
tool_timeout_sec = 120
```

### Hooks

Hooks wire arbitrary shell commands into Vibe's lifecycle to gate, audit, or rewrite agent behavior. No flag is required — declaring a hook is enough.

Declared in `<project>/.vibe/hooks.toml` (project, loaded first; trusted only) and `~/.vibe/hooks.toml` (user-global, loaded second; duplicates by `name` lose to the project entry):

```toml
[[hooks]]
name = "deny-rm-rf"
type = "pre_tool"
match = "bash"                       # tool-name matcher (fnmatch glob + `re:` regex escape, case-insensitive)
command = "uv run python /path/to/guard-bash"
timeout = 60.0                       # seconds; default 60 for all hooks
strict = false                       # tool hooks only: turn failures into denials (pre) / text-clears (post)
description = "Reject dangerous shell commands."
```

Subagents inherit the parent's hook config so policies apply transitively.

#### Common ground

Every hook is spawned with a JSON invocation on **stdin** (UTF-8) containing the session context: `session_id`, `parent_session_id`, `transcript_path`, `cwd`, plus `hook_event_name` discriminating the hook type. Tool hooks add tool-specific fields (below).

Every hook signals back via its **exit code** and **stdout**. The contract on stdout is strict: either empty (do nothing), or a JSON object matching the schema below. Use **stderr** for diagnostics / debug logs.

- **Exit `0`, empty stdout** — passthrough.
- **Exit `0`, valid JSON object on stdout** — structured response. Universal top-level fields:
  - `system_message` (string, optional) — shown to the user in the UI.
  - `decision` (`"allow"` | `"deny"`, optional, default `"allow"`) — the effect of `"deny"` depends on the hook type.
  - `reason` (string, optional) — accompanies `decision: "deny"`.
  - Event-specific payload under `hook_specific_output`.
- **Exit `0`, non-empty but non-conforming stdout** (free-form text, broken JSON, JSON scalar/array, schema mismatch) — treated as a hook failure with the parse error as the message. Warning by default; escalated to deny / clear under `strict = true` on a tool hook.
- **Any non-zero exit / timeout / spawn failure** — same failure path. Diagnostic taken from stderr (falling back to stdout, then the exit code).

Unknown JSON fields are tolerated at every level (forward-compatible). Fields that aren't meaningful for the current hook type are silently ignored.

#### `post_agent`

Fires after every assistant turn that ends without pending tool calls.

- **Receives** (in addition to the session context): no extra fields.
- **Can return**:
  - `decision: "deny"` + `reason` — `reason` is injected as a new user message asking for a retry. Capped at **3 retries per hook per user turn**; further denies become terminal warnings.
  - `system_message` — UI-only.

#### `pre_tool`

Fires per tool call, **before** the user permission prompt. First deny short-circuits remaining `pre_tool` hooks for that call.

- **Receives** (in addition to the session context): `tool_name`, `tool_call_id`, `tool_input` (the model's raw arguments).
- **Can return**:
  - `decision: "deny"` + `reason` — denies the tool call; `reason` becomes the tool error the LLM sees.
  - `hook_specific_output.tool_input` (object) — **full replacement** of the model's arguments. Re-validated against the tool's schema (validation failure → synthesized denial). Rewrites compose left-to-right across hooks. The rewritten arguments are also what the permission prompt displays, what the tool runs with, and what subsequent LLM turns see on the assistant message.
  - `system_message` — UI-only.

#### `post_tool`

Fires per tool call **if and only if the tool body actually ran**. `tool_status` is `success`, `failure`, or `cancelled` (cancellation during the tool body — cancellation is shielded so audit hooks still run). Does not fire when the tool never executed: `pre_tool` denial, user denial at the approval prompt, permission `NEVER`, or cancellation before the body started.

- **Receives** (in addition to the session context): `tool_name`, `tool_call_id`, `tool_input` (post-rewrite), `tool_status`, `tool_output` (structured result dict; null on failure), `tool_output_text` (the running text the LLM will see, mutable by prior hooks), `tool_error`, `duration_ms`.
- **Can return**:
  - `decision: "deny"` + `reason` — replaces `tool_output_text` with `reason`. Pipeline continues; subsequent hooks see the replacement.
  - `hook_specific_output.additional_context` (string) — **appended** (with a `\n` separator) to `tool_output_text`. Composes with a same-hook deny: deny replaces first, then `additional_context` is appended to the replacement.
  - `system_message` — UI-only.

### Session Management

#### Session Continuation and Resumption

Vibe supports continuing from previous sessions:

- **`--continue`** or **`-c`**: Continue from the most recent saved session
- **`--resume`**: Open an interactive session picker
- **`--resume SESSION_ID`**: Resume a specific session by ID (supports partial matching)
- **`/resume`** or **`/continue`**: Open the session picker from inside Vibe; press `D` twice to delete a local saved session. The active session cannot be deleted from this picker.

```bash
# Continue from last session
vibe --continue

# Open session picker
vibe --resume

# Resume specific session
vibe --resume abc123
```

Session logging must be enabled in your configuration for these features to work.

The first user message pins the resolved model to that session. Resuming keeps
the pinned model even if your configured default changes. `/model` uses the
normal config persistence target and updates an existing session override
immediately; the session file is synchronized when the next user message is
sent. An explicit persistent target selected through `/config` changes only
that layer. `/clear` starts a new, unpinned conversation that follows the
current config. If a selected model is no longer configured, Vibe falls back
to the current default model.

#### Working Directory Control

Use the `--workdir` option to specify a working directory:

```bash
vibe --workdir /path/to/project
```

This is useful when you want to run Vibe from a different location than your current directory.

Use `--add-dir` (repeatable) to make additional directories available to the agent for the duration of the session:

```bash
vibe --add-dir /path/to/other-project --add-dir /path/to/library
```

Each path is implicitly trusted (no trust prompt) and contributes its `AGENTS.md` and `.vibe/` configuration (tools, skills, agents, prompts, hooks) to the session. File-tool permissions treat each `--add-dir` path the same way as your primary working directory — reads and writes inside them don't require the "outside workdir" prompt. Nested paths collapse: passing `/repo` and `/repo/sub` is equivalent to passing just `/repo`.

Use `--worktree NAME` to create (or reuse) a [git worktree](https://git-scm.com/docs/git-worktree) and run inside it:

```bash
vibe --worktree my-feature
```

The worktree lives under `$VIBE_HOME/worktrees/<repo-name>-<repo-hash>/NAME` and is checked out on a branch named `NAME` (created if it doesn't exist, attached if it does). Vibe `cd`s into it before the session starts and trusts it for the session (no trust prompt). If you start Vibe from a subdirectory, Vibe enters the matching subdirectory inside the worktree.

Existing worktrees are reused only when they belong to the same git repository and are checked out on branch `NAME`; otherwise Vibe exits with an error instead of running in the wrong checkout.

Pass `--worktree` with no name to have Vibe name one for you:

```bash
vibe "Fix the login bug" --worktree     # -> fix-the-login-bug, on vibe/fix-the-login-bug
vibe --worktree                         # no prompt -> a random slug, e.g. brave-quiet-otter
```

The name comes from your prompt, shortened to whole words. Without a prompt — or when the prompt has nothing usable in it, such as emoji only — Vibe generates a random slug instead. Unlike the named form, this never reuses an existing worktree: Vibe claims a free name, adding `-2`, `-3` and so on if needed, so two sessions started at once can never land in the same checkout. The branch is always `vibe/<name>`, matching the worktrees Le Chat Desktop creates.

Order matters, because `--worktree` takes an optional value: `vibe --worktree "Fix the login bug"` reads the prompt as the *name*. Put the prompt first, or separate it with `--`:

```bash
vibe --worktree -- "Fix the login bug"
```

Automatic cleanup only applies to worktrees Vibe created this run, and only after a session actually started — a startup failure (bad config, `--continue` with no sessions) never deletes anything, and a reused worktree is always left in place. When an interactive session exits, Vibe removes the worktree directory automatically if there are no uncommitted changes, untracked files, or commits beyond the commit where the worktree session started. If any of those exist, Vibe asks whether to keep or remove the worktree. When Vibe created the branch it is deleted alongside the worktree; a branch that already existed and was merely attached is kept unless you confirm its deletion. Keeping preserves the directory and branch so you can return later; removing force-deletes them, discarding changes, untracked files, and commits. Programmatic runs (`vibe -p ... --worktree NAME`) do not clean up automatically because there is no exit prompt; remove them manually with `git worktree remove`. `--worktree` is ignored with `--setup` and `--check-upgrade`.

Sessions are scoped per directory, so `-c`/`--continue` and the `--resume` picker only see sessions started inside that worktree. To carry a session across worktrees, resume it explicitly by ID with `--resume <ID>`.

#### Worktree ownership

Whichever way a worktree is created, Vibe writes an ownership record beside it under `$VIBE_HOME/worktrees/.claims/<repo-name>-<repo-hash>/<name>/`, recording the branch, the commit the session started from, and whether Vibe created the branch. Nothing is ever removed without one: a worktree you made yourself, or one whose record is missing or unreadable, is left alone.

The record directory also holds a marker per session currently working in the worktree. Sessions from different clients run in separate processes with nothing shared between them, so a marker is the only evidence that someone else is still in there. A worktree with any marker left is kept. A process killed outright leaves its marker behind and the worktree survives, which is the direction worth failing in.

The app-server never removes a worktree on its own. Closing a session does not count: the desktop app releases an idle session's process a second after each turn to reclaim it, and the session stays live and resumable, so its worktree outlives that. A worktree that exists is removed in exactly one situation — **you delete its session**. It still has to be one Vibe created, held by nobody else, and free of uncommitted changes, untracked files, and commits made since the session began; anything else is kept and logged.

Two things are cleaned up without asking, neither of which is a worktree you could have worked in. A session whose very first turn never completed has its worktree rolled back, because such a session is never published and leaves no session file — there is nothing to return to. And a reservation that never became a worktree, an empty directory left by a claim whose `git worktree add` did not land, is discarded the next time a session starts in that repo.

The cost of that conservatism is that a worktree whose app-server was killed outright stays on disk, holding a marker for a session that no longer exists. Removing it is a judgement about whether you are finished with the work, which only you can make.

### Update Settings

Upstream Vibe checks PyPI at most once per day during a session. When a newer version is found, the next launch can offer an upstream package update. For this Robo source branch, keep `enable_update_checks = false` and update the intended Git branch explicitly after preserving local work; a PyPI/Homebrew Vibe upgrade is not an update of this Robo branch.

Run `vibe --check-upgrade` to check PyPI immediately, prompt to install a newer version if one exists, and exit.

To disable the daily check entirely, add this to your `config.toml`:

```toml
enable_update_checks = false
```

### Notification Settings

Vibe can notify you when the agent needs your attention (awaiting approval, asking a question, or task complete). This is useful when you switch to another window while the agent works.

To disable notifications:

```toml
enable_notifications = false
```

### Custom Vibe Home Directory

By default, Vibe stores its configuration in `~/.vibe/`. You can override this by setting the `VIBE_HOME` environment variable:

```bash
export VIBE_HOME="/path/to/custom/vibe/home"
```

This affects where Vibe looks for:

- `config.toml` - Main configuration
- `.env` - API keys
- `connector_bootstrap_cache.json` - Short-lived connector discovery cache
- `agents/` - Custom agent configurations
- `prompts/` - Custom system and compaction prompts
- `tools/` - Custom tools
- `logs/` - Session logs

Custom tools will be deprecated in a future release. Prefer skills for new
extensions; Vibe can help migrate existing custom tools to skills.

### Logging

Vibe writes structured logs to `~/.vibe/logs/vibe.log`. Use `/log-level` to open an interactive picker that lets you set the session override and/or persist a level to `config.toml`. You can also set `log_level` directly in `config.toml` or via the `/config` screen.

Valid levels: `DEBUG`, `INFO`, `WARNING` (default), `ERROR`, `CRITICAL`.

Precedence: session override > `LOG_LEVEL` env var > `log_level` in config.toml > default.

The `LOG_LEVEL` environment variable overrides the config value at startup. Use `DEBUG_MODE=true` to force `DEBUG` at startup.

## Editors/IDEs

Robo can be used in text editors and IDEs that support [Agent Client Protocol](https://agentclientprotocol.com/overview/clients). See the [ACP Setup documentation](docs/acp-setup.md) for setup instructions for various editors and IDEs.

## Resources

- [CHANGELOG](CHANGELOG.md) - See what's new in each version
- [CONTRIBUTING](CONTRIBUTING.md) - Guidelines for feature requests, feedback and bug reports

## Data collection & usage

Use of Vibe is subject to our [Privacy Policy](https://legal.mistral.ai/terms/privacy-policy) and may include the collection and processing of data related to your use of the service, such as usage data, to operate, maintain, and improve Vibe. You can disable telemetry and crash reporting in your `config.toml` by setting `enable_telemetry = false`.


## License

Copyright 2025 Mistral AI

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the [LICENSE](LICENSE) file for the full license text.
