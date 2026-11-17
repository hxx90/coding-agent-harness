# Linux worker 与宿主驱动扩展

为接入 BP/CaP-X，保留 CodingSession、独立宿主协议、ProgramVersion、ExecutionJob、
增量事件和未知动作核对，补齐以下通用机制。此前 devlog 中的 Linux 不支持描述是历史状态。

## 执行隔离

Linux worker 在运行用户模块前安装 Landlock 文件内容白名单和 seccomp 系统调用白名单。
允许读取冻结 runtime、程序包、必要系统动态库与随机源；无工作区、宿主数据库或 proc
内容读取权限。禁止网络、fork/exec、向其他进程发信号、元数据写入及未授权系统调用。
安装失败时 worker 失败退出，不回退到无隔离执行。固定宿主 liveness 线程在安装前启动，
seccomp 使用 TSYNC。墙钟、CPU、RSS、日志和动作预算继续由现有运行时执行。

实测环境：Linux 5.15 x86_64、Landlock ABI 1、系统 libseccomp.so.2、Python 3.12。
代码包含 aarch64 的同号 Landlock syscall 路径，但尚未做该平台实机验证。
此平台 bubblewrap 的 mount namespace 被拒绝，未依赖它绕过隔离。

## 显式依赖

宿主可通过 `packages=("numpy",)` 批准已安装的包。程序 manifest 仍需声明
`dependencies={"numpy":"*"}` 或精确版本。`*` 只选择宿主已批准的安装，不联网安装；
实际版本、全部包文件（含 native extensions）的内容哈希进入不可变 runtime 身份。
运行前复核哈希。未声明的包不在 worker 导入路径中，默认依然只有 stdlib。

## 驱动边界

`Runtime` / `host.serve` 接受 operator-owned `hardware_factory(store, media)`，
不向模型暴露加载驱动的工具。`limits` 是宿主预算上限；manifest 只能进一步收紧。
发现接口返回允许依赖、预算与验证语义。

`max_jobs` 是宿主跨会话/重启的 execution 总预算。达到上限仍允许同 request_id 查询和
幂等提交，不创建新任务；`robo_devices.remaining_jobs` 公开剩余次数。

`submission_observation_age_s` 与 `observation_age_s` 默认都为 2 秒。前者只控制程序
准入，后者继续控制所有本地动作。CaP-X 宿主将前者设为 120 秒以容纳模型往返，并保持
后者为 2 秒；不是给旧观测驱动运动的许可。此拆分来自真实 opus-5 的连续 stale submit。
冻结 NumPy 的首次 publish 可能超过 20 秒，因此客户端 publication timeout 单独为
120 秒，仍可取消且无设备执行副作用。

驱动仍提供 discover/observe/evaluate/stop/close。额外的可调用驱动实现：

- `invoke(action, *, deadline, cancelled)`：按已授权操作执行一次；必须遵守绝对 deadline
  和 cancellation event，不隐式重试。参数为 JSON 与有界压缩数值数组，不使用 pickle。
- `reconcile(action)`：只查询原 command ID 的结果，不重新派发。

程序通过 `robo.call(device, operation, *args, **kwargs)` 调用。SDK 自动获取本 job 的
新鲜观察；宿主先校验权限、预算并持久化 action intent，随后释放全局锁执行驱动调用，
所以取消不会被长调用锁住。明确收到 API 异常（`CodingError("device_error", ...)`）
与没有回执不同；后者记录 unknown，禁止后续调用直至原调用被核对。

大数组保留在 action store；事件页只携带动作元数据，图像继续通过 MediaStore 传递，
避免把深度矩阵塞进每一轮模型上下文。

## 反馈与模型预算

事件工具压缩重复的展示字段，并从最近的 observation 中选取图像；原始事件、
command ID、错误和停止证据仍持久保存。`program_status` 提供完整 job 状态。
日志与动作记录使用 SQLite 事务减少提交次数，设备 I/O 不占用数据库事务。
程序可用 `robo.report` 记录有大小上限的阶段反馈，含义见
[阶段报告说明](../../docs/robo-reports.md)。

SDK 的 `Settings.output_limit_retries` 控制连续输出截断后的额外请求次数，
默认 1，设为 0 时不重试。收到完整回复后计数归零；被截断回复中的工具调用
全部拒绝执行。每次恢复仍受总回合、token、时间及 Provider HTTP 请求预算约束。
此选项由 SDK 调用者设置，目前不提供 CLI/TOML 配置入口。

## 验证入口与边界

- `tests/robo/test_extensions.py`：依赖冻结、数组传输、驱动错误、未知回执与取消。
- `tests/robo/test_store_transactions.py`：事务原子性与失败处理。
- `tests/robo/test_event_page.py`、`test_event_wait.py`：事件展示与有界等待。
- `tests/cli/test_session.py`：输出截断恢复、预算和会话生命周期。

Linux 隔离的历史实测平台是 x86_64；aarch64 的 syscall 路径尚无实机验收记录。
这些运行时机制与离线测试不能替代真实机械臂的控制、标定和停止验证。
早期逐轮 CaP-X 成绩不作为当前状态维护在本接口文档中；评测产物保留在
`/workspace/hanyu/roboworld-bench/`，辅助能力回放的结果和限制见
[能力改进说明](../../docs/robo-improvement.md)。
