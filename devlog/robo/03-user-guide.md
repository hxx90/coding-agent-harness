# 本地运行 Robo（萝卜）

定位：**A runtime for agents that act in the physical world.** 本版支持 macOS 上的模拟相机与二维位移台；无需模型 API Key 即可跑完整本地闭环。真实 MHS 尚不可用，见 [核查记录](01-mhs-research.md)。

## 安装和启动

在仓库根目录：

```bash
source .venv/bin/activate
python -m pip install -e '.[dev]'
agent-harness --version
robo --help
export ROBO_HOME="$HOME/.local/share/robo-demo"
robo host --backend sim
```

宿主占用这个终端并持续管理任务。在第二个终端设置同样的 `ROBO_HOME` 后操作：

```bash
source .venv/bin/activate
export ROBO_HOME="$HOME/.local/share/robo-demo"
robo health
robo devices
robo demo
```

`robo demo` 创建独立的 demo-workspace：先写错误方向代码，提交并看到 **exit_code=0、physical_verification=failed**，再修改控制代码，重新发布为不同版本，用像素感知反馈完成对准。它还注入一次丢失动作回执，查询核对后继续。最终 `passed: true`，并显示两个 job、程序版本、误差和验证观察。失败时命令退出码非零。重复演示会从当前模拟现场开始，真实硬件不存在这里的模拟语义。

## 手动闭环

```bash
robo publish examples/robo/align
robo observe
```

复制输出中的版本 ID 与**新鲜**观察 ID（发布版本可先做，观察在提交前做）。示例中的尖括号应替换为实际值：

```text
robo submit <program-version> --request-id align-001 --based-on <observation-id> --parameters '{"gain":0.65}'
robo status <job-id>
robo events <job-id> --follow
robo events <job-id> --after <cursor>
robo lookup align-001
robo action <job-id> <command-id>
```

如果手动复制超过默认 2 秒观察新鲜度，重新 `robo observe`。更方便的连续提交脚本：

```python
import uuid
from pathlib import Path
from agent_harness.robo.cli import default_root
from agent_harness.robo.client import Client
from agent_harness.robo.programs import source_bundle

client = Client(default_root())
source = Path('examples/robo/align')
version = client.request('publish', **source_bundle(source, source / 'program.json'))
scene = client.request('observe')
request_id = 'align-' + uuid.uuid4().hex  # 提交前保存此 ID
job = client.request('submit', program_version=version['id'], request_id=request_id,
                     based_on=scene['id'], parameters={'gain': 0.65})
print(job['id'])
for update in client.subscribe(job['id']):
    print(update)
```

修改示例目录中的 `control.py` 或依赖文件后重新 publish，使用新的 request ID 运行。旧 job 的代码和依赖固定。事件含增量日志、异常、动作、PNG 图像引用与独立验证；`after` 游标可以跨连接保存。程序内可用 SDK 见 [示例代码](../../examples/robo/align/control.py) 与 [架构](02-architecture.md)。

## 取消与断开

```text
robo cancel <job-id>
robo events <job-id> --follow
robo disconnect <job-id>
robo protect
```

取消请求返回 `cancelling` 后继续查询，直到状态终结并有 `stop_confirmation.status=confirmed`。停止模型推理、取消程序、断开客户端和锁存设备保护是不同动作。`protect` 仅为模拟保护锁存，不是实机急停；本版没有自动解除保护的命令。

默认 `continue_bounded`：关闭 agent/客户端终端，job 继续运行到代码结束或时间上限。`--disconnect-policy cancel`：需要持续订阅 events，5 秒没续租即取消。宿主终端应保持运行；Ctrl-C 宿主会受控取消所有任务。宿主异常退出后重启使用同一 `ROBO_HOME`，可查旧任务，它们标记 interrupted，不自动再次运动。

## 用模型现场编程

复用 CLI 2.0.0 的 provider 配置和权限。配置自己的兼容 Chat Completions 服务及支持图像输入的模型后：

```bash
export AGENT_HARNESS_BASE_URL='https://your-provider.example/v1'
export AGENT_HARNESS_MODEL='your-vision-model'
# API Key 按原 CLI 的 auth / --api-key-env 配置
robo run --allow 'robo_*' --allow 'program_*' --allow read --allow write --allow edit \
  '观察模拟设备，编写本地视觉对准程序，提交后根据日志和图像修改，再独立核实物理结果。'
```

也可用 `agent-harness run --robo-home "$ROBO_HOME" ...`，交互模式 `robo chat`。program_run 只提交；program_events 获取反馈；本地程序不依赖持续模型调用。允许这些工具只授予宿主内置模拟范围，无法扩大硬限制。

本机尚未配置真实模型 base URL/model/API Key；测试使用确定性模型协议与本地 HTTP 服务，只验证工具与媒体链路，不代表真实模型已完成现场开发。原生视频格式没有实现；当前按带时间信息的图像帧输入。媒体 export/import 随会话携带，旧 schema v1 文本会话可继续使用。

## 限制

目前是单机、单用户、单模拟工作单元；Linux 执行沙箱、真实 MHS SDK/硬件驱动、持续视频捕获/原生视频协议、实机标定/保护/故障确认均待验证。依赖只支持固定 stdlib + 清单内 vendored Python；无 ambient pip 包。用户自行使用 shell 的本机权限未收紧，因此不能把本版沙箱宣称为整个开发环境的真实硬件隔离方案。
