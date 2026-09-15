# MHS 官方接口核查

核查日期：2026-09-15；通过 HTTPS 实际获取以下官方页面，HTTP 200。

1. [Anthropic: Previewing the Model Hardware Standard](https://www.anthropic.com/news/model-hardware-standard-research-preview)，发布日期 2026-08-27。
2. [MHS 官方网站](https://www.modelhardwarestandard.com/)，由官方公告直接链接。

官方公告明确："we have more work to do on the standard before we open-source it"。官网明确："MHS is starting as a limited research preview"、"Access is by application during the preview"。官网链接只有官方公告与申请表，没有规范版本、SDK 下载、包名或公开仓库/发行标签。公告提到 AWS Strands Robots 的相关包也是提供给预览参与者的 private, pre-release version。

因此，本次**无法获得并固定可用的真实 MHS SDK/规范版本**。这不是证明互联网不存在任何同名包；没有把同名第三方包当作官方实现安装。未自动提交申请表，也没有访问私人预览账户。

## 实现状态

- `hardware.py` 的 `SimHardware` 是 **Robo 自定义测试 adapter**，版本 `robo-sim/0.1`，不是 MHS 兼容层。
- 每个设备、观察、job 和验证结果保留 `simulated` 或明确的 sim 身份。
- `robo mhs-status` 提供来源、核查日期、缺失项，`robo host --backend mhs` 直接返回 `mhs_unavailable`，不静默退回模拟。
- Agent 的短工具请求与本地程序的 SDK 最终调用同一个宿主设备入口；真实 SDK 取得后在此接入，不能将当前模拟方法名视作 MHS 字段。

## 真实集成仍需取得并验证

正式/预览规范和 SDK 的可访问版本与许可、首组相机和运动驱动、硬件身份与标定、操作完成/查询/取消/失联语义、真实执行隔离和设备保护、设备级去重或未知结果核对。首版没有这些条件，也没有模型凭据，故未进行实机或真实模型能力评估。
