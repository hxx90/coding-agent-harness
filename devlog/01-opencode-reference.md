# OpenCode 源码参考与取舍

日期：2026-09-15。仓库：[anomalyco/opencode](https://github.com/anomalyco/opencode)，MIT 许可证。

参考固定在提交 [`228e9095ba3988a02664c3816cb51f98584e86c2`](https://github.com/anomalyco/opencode/tree/228e9095ba3988a02664c3816cb51f98584e86c2)，提交时间为 2026-09-14T07:16:48Z。没有将会变化的 `dev` 分支作为验收依据。

完整浅克隆因网络超时失败；另行按提交 SHA 成功获取并阅读了以下相关源码。所获取文件的字节数和 SHA-256 记录在 [01-opencode-sources.json](01-opencode-sources.json)。本项目借鉴执行模型并独立用 Python 实现，没有把 TypeScript 源码拷贝进运行包。

| 参考源码（均为固定提交） | 阅读到的设计 | 本项目实现与取舍 |
| --- | --- | --- |
| [cli/cmd/run.ts](https://github.com/anomalyco/opencode/blob/228e9095ba3988a02664c3816cb51f98584e86c2/packages/opencode/src/cli/cmd/run.ts) | 单次执行、交互模式、stdin、会话继续与事件输出 | `cli.py` 提供简洁 REPL、`run`、stdin、JSONL；省去独立 HTTP 服务和终端客户端连接协议 |
| [session/processor.ts](https://github.com/anomalyco/opencode/blob/228e9095ba3988a02664c3816cb51f98584e86c2/packages/opencode/src/session/processor.ts) | 流式处理文本、工具调用与结果；执行前快照；中断时清理未结束的工具；重复操作保护 | `CodingSession.run()` 使用持久化调用日志、完整的 call/result 配对、取消和预算；未确认的工具标记为未知，恢复后重新检查现场 |
| [session/session.ts](https://github.com/anomalyco/opencode/blob/228e9095ba3988a02664c3816cb51f98584e86c2/packages/opencode/src/session/session.ts) | 会话与消息具有独立持久身份 | 本地 JSON 会话、原子替换和文件锁；不引入数据库服务 |
| [permission/index.ts](https://github.com/anomalyco/opencode/blob/228e9095ba3988a02664c3816cb51f98584e86c2/packages/opencode/src/permission/index.ts) | 通配符规则、最后匹配优先、allow/ask/deny、单次/持续授权 | 所有 Agent 工具经过 `Permissions`。项目配置只能收紧权限。持续授权仅限本进程的同一操作，不跨重启保存 |
| [session/instruction.ts](https://github.com/anomalyco/opencode/blob/228e9095ba3988a02664c3816cb51f98584e86c2/packages/opencode/src/session/instruction.ts) | 项目指令及读取具体文件时的目录指令 | 根 AGENTS.md 放进请求；目录 AGENTS.md 随文件读取返回。指令文本不参与宿主权限判定 |
| [session/compaction.ts](https://github.com/anomalyco/opencode/blob/228e9095ba3988a02664c3816cb51f98584e86c2/packages/opencode/src/session/compaction.ts) | 有界旧工具结果、保留近期消息和摘要 | 请求上下文采用确定性检查点和完整工具组裁剪；原始会话仍保留。最新用户消息不截断，单个当前交换过大则明确报错 |
| [tool/registry.ts](https://github.com/anomalyco/opencode/blob/228e9095ba3988a02664c3816cb51f98584e86c2/packages/opencode/src/tool/registry.ts) | 根据模式与配置组装工具集合 | 内置工具与扩展统一注册；Plan 隐藏写入、Shell 和 MCP 工具，并在执行处再次检查 |
| [tool/shell.ts](https://github.com/anomalyco/opencode/blob/228e9095ba3988a02664c3816cb51f98584e86c2/packages/opencode/src/tool/shell.ts) | 有界命令输出、超时、中断与进程生命周期 | 独立进程组，限制输出，取消和超时回收进程；明确这是用户授权的本地执行，不是 OS 沙箱 |
| [tool/task.ts](https://github.com/anomalyco/opencode/blob/228e9095ba3988a02664c3816cb51f98584e86c2/packages/opencode/src/tool/task.ts) | 子任务使用独立会话并派生权限 | 提供同步的只读探索子会话，最多 8 次请求，计入父任务预算；禁止写入、Shell、MCP 和递归委派 |
| [skill/index.ts](https://github.com/anomalyco/opencode/blob/228e9095ba3988a02664c3816cb51f98584e86c2/packages/opencode/src/skill/index.ts) | 按需发现、列出并加载 SKILL.md | 支持项目和用户 Skill 目录，按需加载正文和受限相对资源；只解析 name、description 前置元数据 |

另外获取了 `session/prompt.ts`、`cli/cmd/run/entry.body.ts`。`permission/evaluate.ts` 只是 re-export，实际权限逻辑阅读的是 `permission/index.ts`。

取舍的目标是完整覆盖本仓库 [R01–R14](00-scope-and-plan.md) 的编码工作链路，不宣称复刻 OpenCode 全部产品功能。当前不包含全屏 TUI、远程会话服务、IDE/LSP 集成、后台并行子 Agent 或多模态附件。MCP 限定为 stdio 工具协议，不提供资源订阅、模型采样或远程 HTTP 传输。
