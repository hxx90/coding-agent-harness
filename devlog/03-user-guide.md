# 使用手册

## 安装与快速开始

需要 Python 3.11+。CLI 不需要启动网站或安装 Streamlit，也不要求项目预先存在 `.agent-harness.json`。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
agent-harness --help
agent-harness --version
```

配置一个支持 Chat Completions 工具调用的服务。`BASE_URL` 通常以 `/v1` 结尾，不要填写网页地址。

```bash
export AGENT_HARNESS_BASE_URL="https://your-model-service.example/v1"
export AGENT_HARNESS_MODEL="your-model-id"
agent-harness auth login
agent-harness models
agent-harness doctor
```

`auth login` 隐藏输入，凭据写在用户配置目录中，权限为 0600。也可以设置 `AGENT_HARNESS_API_KEY`，它优先于存储的凭据。不要把真实 Key 放在命令行参数、项目文件或提交记录中。

```bash
# 交互模式
agent-harness -C /path/to/project

# 只读分析，适合第一次接触项目
agent-harness -C /path/to/project run --mode plan "解释这个项目的主要入口和模块关系"

# 一次编码任务：文件写入、编辑和固定验证得到显式授权
agent-harness -C /path/to/project run \
  --allow write --allow edit --allow test \
  --test-command "python -m pytest -q" \
  "修复用户名为空时仍被接受的问题，并运行测试"

# 从 stdin 读取，输出每行一个 JSON 事件
printf '列出项目的主要模块\n' | agent-harness -C /path/to/project run --json

# 继续当前项目最近的会话
agent-harness -C /path/to/project run --session latest "继续处理上次未完成的部分"
```

交互时需要确认的操作会显示工具名和具体命令/修改内容，输入 `y` 允许一次、`s` 允许当前进程内完全相同的操作、`n` 拒绝。非交互进程不能等待确认，缺少授权会停止并返回 3。

`--yes` 表示授权当前任务的本地操作。Shell 和 MCP 是可信本地执行，能够产生项目外或不可撤销的副作用；文件工具的范围检查不构成 Shell 沙箱。显式 deny 和 Plan 限制仍然优先。

## 交互命令

| 命令 | 功能 |
| --- | --- |
| `/help` | 查看命令列表 |
| `/new` | 开始新会话 |
| `/sessions`、`/resume ID` | 列出与恢复当前项目会话 |
| `/status` | 查看会话 ID、模式、模型和用量 |
| `/mode plan`、`/mode build` | 切换只读规划与编码模式 |
| `/model MODEL_ID` | 对后续请求切换模型 |
| `/attach relative/path` | 为下一条消息附加项目内 UTF-8 文本文件 |
| `/diff` | 查看上一轮文件变更 |
| `/undo`、`/redo` | 按轮撤销或重做文件、对话和待办 |
| `/compact` | 下一次请求主动压缩早期上下文 |
| `/todos`、`/skills` | 查看待办与可用 Skills |
| `/export /path/session.json` | 导出对话，无文件恢复权限和私有推理 |
| `/exit`、Ctrl+D | 退出，保留会话 |
| Ctrl+C | 取消活动模型请求或命令，保留已产生的变更 |

连接配置仍来自当前配置文件/环境/参数，恢复会话不会恢复以前的 API Key。`-C` 必须与该会话的项目一致。会话 ID 可以使用至少四位、能唯一匹配的十六进制前缀。

```bash
agent-harness sessions list
agent-harness sessions list --all
agent-harness sessions show SESSION_ID
agent-harness sessions export SESSION_ID /tmp/session.json
agent-harness -C /path/new-project sessions import /tmp/session.json
agent-harness sessions delete SESSION_ID
agent-harness -C /path/to/project diff SESSION_ID
agent-harness -C /path/to/project undo SESSION_ID
agent-harness -C /path/to/project redo SESSION_ID
```

撤销会先检查外部修改冲突。导入会话不会执行先前的工具，也不能撤销导出来源的文件。操作中途崩溃而尚未确认归属的变更只提供 Diff，自动撤销会拒绝，需检查后手动恢复。不要编辑会话内部 JSON 来手动制造撤销记录。

## 配置文件

用户配置：`${XDG_CONFIG_HOME:-~/.config}/agent-harness/config.toml`。用 `AGENT_HARNESS_CONFIG` 指定替代路径。项目配置：项目根目录 `.agent-harness.toml`。`agent-harness config path` 显示实际路径，`config show` 显示脱敏后的解析结果。

```toml
[provider]
base_url = "https://your-model-service.example/v1"
model = "your-model-id"
api_key_env = "AGENT_HARNESS_API_KEY"
timeout = 60
max_output_tokens = 8192
stream = true

[agent]
mode = "build"
max_turns = 40
max_tokens = 250000
max_seconds = 1800
context_chars = 160000
command_timeout = 120
test_command = ["python", "-m", "pytest", "-q"]
```

`test_command` 使用参数数组执行，不经过 Shell；也可写字符串，由 `shlex` 解析。`python`/`python3` 自动使用 CLI 所在的 Python 环境。项目有自己的虚拟环境时可配置它的绝对解释器路径。没有测试配置也可正常问答或编码，但结果会注明 `verification=not_configured`。

配置多个服务：

```toml
[providers.work]
base_url = "https://work-model-service.example/v1"
model = "work-model"
api_key_env = "WORK_MODEL_KEY"
```

用 `--profile work` 选择，或 `agent-harness auth login --profile work` 保存该 profile 的 Key。`--base-url`、`--model`、`--api-key-env`、`--max-turns`、`--max-tokens`、`--max-seconds` 可覆盖配置。

只有用户配置可以持久化 allow：

```toml
[[permissions.rules]]
tool = "read"
pattern = "*"
action = "allow"

[[permissions.rules]]
tool = "bash"
pattern = "*"
action = "ask"

[[permissions.rules]]
tool = "webfetch"
pattern = "*"
action = "deny"
```

规则按顺序取最后匹配项。文件工具的 pattern 匹配路径，bash 匹配 `工作目录: 命令`，apply_patch 匹配补丁正文，MCP 匹配参数 JSON。命令行 `--allow TOOL` / `--deny TOOL` 可重复使用。项目中的 allow 配置会直接报配置错误；项目仅能增加 ask/deny。

旧 `.agent-harness.json` 中的 `editable_paths`、`protected_paths`、`sensitive_paths` 和固定验证命令仍然有效。`.env*`、`.git`、常见依赖目录、项目配置文件和符号链接具有额外保护。

## 工具与扩展

内置工具包括 `read`、`glob`、`grep`、`write`、`edit`、`apply_patch`、`bash`、`test`、`todo_read`、`todo_write`、`question`、`webfetch`。`grep` 使用字面量搜索。`edit` 要求唯一匹配，除非明确设置 replace_all。统一补丁支持新建和修改，删除/重命名可通过获准的 Shell 完成。

读取已有文件后才能编辑；文件在读取后被外部修改会拒绝覆盖。读取时会返回从根到该文件目录的 AGENTS.md。项目指令与 Skill 内容只影响任务上下文，不能授予工具权限。

Skills 搜索位置：

```text
PROJECT/.agents/skills/NAME/SKILL.md
~/.config/agent-harness/skills/NAME/SKILL.md
```

同名时项目优先。支持可选的 `name` / `description` frontmatter（单行文字或缩进的多行描述）；正文为 Markdown。模型通过 `skill` 工具按需加载，支持读取 Skill 目录内相对文本资源。每份内容最多 64,000 字节，不跟随资源符号链接。

`task` 工具创建独立的只读探索会话，最多 8 次模型请求，消耗父任务剩余预算。子会话没有写入、Shell、MCP 或再次创建子任务的能力。

MCP stdio 示例：

```toml
[mcp.docs]
command = ["/absolute/path/to/python", "/absolute/path/to/mcp_server.py"]
timeout = 30
# 只有明确需要时配置传给 MCP 的环境变量；config show 会遮盖值。
# [mcp.docs.env]
# SERVICE_TOKEN = "..."
```

支持初始化、分页 tools/list 和 tools/call，采用每行一个 JSON-RPC 消息的 stdio 协议。服务器 stdout 必须仅输出协议消息，stderr 不进入模型和日志。先授权 `mcp_start` 启动具体命令，再分别授权暴露出的 `mcp_docs_TOOL`。Plan 不启动 MCP。对服务器发起的模型采样或交互请求返回“不支持”，避免旁路主程序权限。

## 结果与限制

| 退出码 | 含义 |
| --- | --- |
| 0 | 对话轮正常完成；查看 verification 判断验证情况 |
| 1 | 配置、模型、工具、上下文或持久化错误 |
| 2 | 命令行用法错误 |
| 3 | 需要交互授权或用户输入 |
| 4 | 请求、Token 或时间预算耗尽 |
| 130 | 用户取消 |

JSONL 包括 `session_started`、`text_delta`、`response_finished`、`tool_started`、`tool_finished`、`validation_started`、`validation_finished`、`error`、`result` 等事件。最终 `result` 带 session_id、status、text、turns、usage、verification 和 error。stdout 的每一行都是 JSON，适合脚本逐行消费；非任务命令返回单个 JSON 值。

预算在每次模型请求前检查。Token 按服务返回统计；缺失时用文本长度估算，不是计费账单。当前响应可能使累计用量超过阈值，程序会在下一次请求前停止。命令默认最多 120 秒，单条 bash 可指定 0.1–600 秒；stdout/stderr 各最多保留 64 KiB。POSIX 会回收进程组；其他系统仅使用 Python 的直接进程终止能力，尚未做跨平台端到端验收。

文件读写上限 1 MiB；快照单文件 8 MiB，总快照 100 MiB；单个会话文件上限 512 MiB；可见文件上限 5,000。文本附件每个 64,000 字符，最多 10 个，任务与附件合计最多 256,000 字符。项目很大或当前工具交换超过上下文限额时会明确报错。

快照只能恢复纳入跟踪的普通项目文件，不能恢复 Shell 的网络、数据库、系统配置或其他目录副作用。显式编辑的忽略文件会跟踪；仅由 Shell 改写的忽略文件、超大文件和受保护路径不保证可撤销。等待模型期间的外部编辑不会记为 Agent 变更；Shell/MCP 执行窗口内的外部并发写入无法可靠区分，避免同时修改同一工程。外部编辑与 Agent 并行工作时应注意冲突提示。

## 可选网页与开发验证

```bash
python -m pip install -e '.[web]'
streamlit run app.py --server.address localhost
```

旧网页仍使用隔离副本和旧任务验收流程，自主执行依赖 macOS `sandbox-exec`。它和 CLI 的工作区信任模型不同。

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m mypy
python -m ruff check src/agent_harness/coding src/agent_harness/cli.py src/agent_harness/sandbox.py tests/cli
python -m build
```

完整验证记录见 [04-validation.md](04-validation.md)，架构见 [02-architecture.md](02-architecture.md)。
