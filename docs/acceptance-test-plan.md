# Coding Agent Harness MVP 验收方案

> 版本说明：本文的 E01～E06 和审批页面清单保留为早期回归基线。MVP 首版的自主循环见 [自主模式实现与边界](./autonomous-mode-mvp.md)；从零建站入口、逐游戏启动验收和人工试玩边界见 [从零创建网站](./zero-code-website.md)。

| 文档信息 | 内容 |
| --- | --- |
| 文档版本 | 首版交付整理（对应 Harness v1.0.0） |
| 状态 | 自动化验收已执行，产品体验待执行 |
| 更新时间 | 2026-08-05 |
| 对应产品文档 | [Coding Agent Harness MVP PRD](./product-proposal.md) |
| 对应技术文档 | [Harness 技术设计](./harness-technical-design.md) |

## 0. 文档目的

本文把“做出来”转化为可以重复执行和明确判断的验收用例。

验收分为三层：

1. **确定性验收**：使用 ScriptedModelAdapter、DemoModelAdapter 或模拟工具结果，验证状态机、权限、预算、停止、继续、Trace、撤销和 E01/E02 修复路径；
2. **API 页面验收**：验证未配置模型 API 时不能启动任务，配置完成后可进入真实模型任务流程；
3. **外部模型探索**：使用通用 OpenAI-compatible 模型重复运行代表任务，判断方向价值。这不等于接入目标模型。

确定性 Adapter 只用于开发和自动化验收，不出现在产品页面；产品任务必须连接模型 API。

---

## 1. 验收环境

### 1.1 基础环境

| 项目 | 要求 |
| --- | --- |
| Python | 3.11 或以上 |
| 测试框架 | pytest |
| 应用 | 本地 Streamlit |
| 模型 | 工程回归：ScriptedModelAdapter + DemoModelAdapter；页面任务：OpenAI-compatible 外部模型 API |
| 操作系统 | MVP 开发机当前系统 |
| 网络 | 确定性工程回归不需要；页面任务和外部模型试跑需要 |
| 数据 | 每个用例使用独立临时 Workspace 和 Runtime Data |

### 1.2 Fixture 使用规则

验收仓库建议采用：

```text
tests/
├── fixtures/
│   ├── e01_bugfix/
│   └── e02_feature/
├── integration/
│   ├── test_agent_repair.py
│   ├── test_permission_and_revert.py
│   ├── test_budget_and_continue.py
│   └── test_stop.py
└── reports/
```

每次运行前：

1. 把原始 Fixture 复制到新的临时目录；
2. 以临时目录作为 Workspace；
3. 为本次用例创建独立 Runtime Data；
4. 运行结束后保存 Trace、最终 Diff 和验收结果；
5. 不直接修改 `tests/fixtures` 中的原始文件。

### 1.3 通用项目配置

除用例另有说明外，Fixture 使用：

```json
{
  "schema_version": 1,
  "validation_command": [
    "python",
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider"
  ],
  "validation_timeout_seconds": 30,
  "editable_paths": ["src"],
  "protected_paths": [
    "tests",
    ".agent-harness.json"
  ],
  "sensitive_paths": [
    ".git",
    ".env"
  ]
}
```

---

## 2. 验收结果定义

### 2.1 单次任务通过

一个端到端任务同时满足以下条件，才记录为“通过”：

- 用户任务使用本文固定文本；
- 未人工修改代码或替模型补写方案；
- 最近一次固定 pytest 退出码为 0；
- 多游戏网站的 manifest 全部游戏通过沙箱内首次启动检查，无同步异常或空白舞台；
- 保护文件哈希未变化；
- Workspace 外没有产生 Agent 文件修改；
- 人工检查 Diff 符合任务要求；
- 没有明显无关修改；
- Trace 完整且不包含模型凭证；
- 最终状态为 `success`。

只满足 pytest 通过但 Diff 不符合需求，记录为“验证通过但任务不通过”。

### 2.2 单次任务失败分类

| 分类 | 定义 |
| --- | --- |
| `task_understanding` | 错误理解用户任务或规则 |
| `code_navigation` | 未找到相关代码或读取不足 |
| `code_generation` | 代码实现错误 |
| `tool_selection` | 选择了错误工具或错误顺序 |
| `tool_arguments` | 工具参数持续错误 |
| `validation_repair` | 看到了测试错误但无法修复 |
| `permission` | 尝试越权且未自我纠正 |
| `budget` | 达到预算仍未完成 |
| `model_api` | 模型接口、限流或服务错误 |
| `harness` | 状态、工具、快照、Diff、撤销或 Trace 实现错误 |
| `environment` | 项目依赖或测试环境错误 |

失败时必须选择一个主分类，可以补充多个次分类。

---

## 3. E01：修复已有代码问题

### 3.1 验收目的

验证 Harness 能否驱动模型适配层：

- 根据任务定位代码；
- 在批准后修复 Bug；
- 不修改受保护测试；
- 运行 pytest 并以测试通过结束。

### 3.2 初始项目

目录：

```text
e01_bugfix/
├── .agent-harness.json
├── README.md
├── src/
│   ├── __init__.py
│   └── user.py
└── tests/
    └── test_user.py
```

`README.md`：

```markdown
# User validation

`validate_username` 接收用户名并返回原值。

当用户名是 `None`、空字符串或只包含空白字符时，
必须抛出 `ValueError("username is required")`。
```

`src/__init__.py`：

```python
```

`src/user.py` 初始内容：

```python
def validate_username(username: str | None) -> str:
    if username is None:
        raise ValueError("username is required")
    return username
```

`tests/test_user.py`：

```python
import pytest

from src.user import validate_username


@pytest.mark.parametrize("username", [None, "", "   "])
def test_rejects_missing_username(username):
    with pytest.raises(ValueError, match="username is required"):
        validate_username(username)


def test_returns_valid_username():
    assert validate_username("alice") == "alice"
```

### 3.3 基线结果

运行固定 pytest：

- 共 4 个参数化/普通测试；
- `None` 用例通过；
- `""` 和 `"   "` 用例失败；
- `"alice"` 用例通过；
- 总体退出码非 0。

### 3.4 固定用户输入

```text
用户注册时，空用户名没有返回错误。请定位并修复。
None、空字符串和只包含空白字符都应视为无效。
运行现有测试验证，不要修改无关功能，也不要修改测试。
```

### 3.5 期望计划至少包含

- 任务是补全用户名必填校验；
- 相关文件包括 `src/user.py`；
- 预计只修改 `src/user.py`；
- 使用项目固定 pytest 验证。

计划文字不要求逐字一致，但不能误解为修改测试。

### 3.6 通过条件

- 用户批准前所有 Workspace 文件哈希不变；
- 最终 `src/user.py` 同时拒绝 `None`、`""` 和纯空白；
- `validate_username("alice")` 仍返回 `"alice"`；
- `tests/test_user.py` 哈希不变；
- 最终固定 pytest 退出码为 0；
- 最终 Diff 只包含必要的 `src/user.py` 修改；
- Trace 包含计划、批准、Patch、Validation 和 `success`。

---

## 4. E02：完成小型开发任务

### 4.1 验收目的

验证 Harness 能否驱动模型适配层：

- 从 README 和测试理解明确业务规则；
- 在已有模块中新增函数；
- 处理边界值；
- 完成修改和测试闭环。

### 4.2 初始项目

目录：

```text
e02_feature/
├── .agent-harness.json
├── README.md
├── src/
│   ├── __init__.py
│   └── order.py
└── tests/
    └── test_order.py
```

`README.md`：

```markdown
# Order discount

在 `src/order.py` 中实现：

`calculate_discount(subtotal: float) -> float`

函数返回折扣金额，而不是折后总价。

规则：

- subtotal 小于 0：抛出 `ValueError("subtotal must be non-negative")`
- subtotal 小于 100：折扣为 0
- subtotal 大于等于 100 且小于 500：折扣为 subtotal 的 5%
- subtotal 大于等于 500：折扣为 subtotal 的 10%
- 返回值保留两位小数，使用 Python `round(value, 2)`
```

`src/__init__.py`：

```python
```

`src/order.py` 初始内容：

```python
"""Order domain helpers."""
```

`tests/test_order.py`：

```python
import pytest

from src.order import calculate_discount


@pytest.mark.parametrize(
    ("subtotal", "expected"),
    [
        (0, 0.0),
        (99.99, 0.0),
        (100, 5.0),
        (499.99, 25.0),
        (500, 50.0),
        (1234.56, 123.46),
    ],
)
def test_calculate_discount(subtotal, expected):
    assert calculate_discount(subtotal) == expected


def test_rejects_negative_subtotal():
    with pytest.raises(ValueError, match="subtotal must be non-negative"):
        calculate_discount(-0.01)
```

### 4.3 基线结果

由于 `calculate_discount` 尚不存在：

- pytest 收集测试时 ImportError；
- 总体退出码非 0。

### 4.4 固定用户输入

```text
请在订单模块增加 calculate_discount 函数，
严格按照 README 中的折扣规则实现，并运行现有测试。
不要修改 README 和测试，不要增加任务之外的功能。
```

### 4.5 期望计划至少包含

- 阅读 README 和 `src/order.py`；
- 在 `src/order.py` 新增 `calculate_discount`；
- 处理负数、两个金额分界和两位小数；
- 运行固定 pytest；
- 预计只修改 `src/order.py`。

### 4.6 通过条件

- 用户批准前所有文件哈希不变；
- 只修改 `src/order.py`；
- 函数签名与 README 一致；
- 所有规则和边界值符合测试；
- README 和测试哈希不变；
- 最终 pytest 退出码为 0；
- Trace 包含完整 Task、Tool Call、Validation 和成功状态。

---

## 5. E03：自主修复循环

### 5.1 验收目的

确定性验证 Agent 不是“写一次就结束”，而是在第一次测试失败后读取错误、继续修改并再次验证。

该用例使用 `e01_bugfix` Fixture 和 `ScriptedModelAdapter`，不依赖外部模型是否偶然一次写对。

### 5.2 模拟响应序列

| Turn | 阶段 | FakeModel 行为 | 预期结果 |
| --- | --- | --- | --- |
| 1 | Analyze | `read_file("src/user.py")` | 返回初始代码 |
| 2 | Analyze | `submit_plan(...)` | 进入等待批准 |
| - | Approval | 测试代码批准 | 进入 Execute |
| 3 | Execute | Patch：只增加 `username == ""` 判断 | Patch 成功 |
| 4 | Execute | `run_validation()` | 空白字符串测试仍失败，进入 Repair |
| 5 | Repair | Patch：改为 `username is None or not username.strip()` | Patch 成功 |
| 6 | Repair | `run_validation()` | pytest 通过，进入 Success |

### 5.3 必须出现的状态序列

```text
idle
→ analyzing
→ awaiting_approval
→ executing
→ validating
→ repairing
→ validating
→ success
```

允许在 `executing` 或 `repairing` 内出现额外的只读工具事件，但不能跳过第一次失败后的 `repairing`。

### 5.4 通过条件

- 同一 Run 中至少出现两次 `apply_patch`；
- 同一 Run 中恰好出现两次或以上 Validation；
- 第一次 Validation 非 0；
- 第一次失败后没有用户补写代码或重新批准；
- 第二次 Validation 为 0；
- 最终状态为 `success`；
- Trace 能还原完整状态转换。

---

## 6. E04：权限、快照和撤销

### 6.1 验收目的

确定性验证：

- Agent 不能修改保护路径；
- Agent 不能越出 Workspace；
- 撤销只移除 Agent 变更；
- 用户任务开始前已有的代码变化得到保留。

### 6.2 前置准备

以 `e01_bugfix` 为基础：

1. 复制到临时 Workspace；
2. 在开始 Task 前，由测试代码在 `src/user.py` 第一行增加：

```python
# user change before agent task
```

3. 记录整个 Workspace 哈希；
4. 创建 Task Snapshot。

### 6.3 操作与预期

| 步骤 | 操作 | 预期 |
| --- | --- | --- |
| 1 | 尝试 Patch `tests/test_user.py` | 拒绝，错误码 `path_protected` |
| 2 | 尝试新建 `../outside.txt` | 拒绝，错误码 `path_outside_workspace` |
| 3 | 合法修改 `src/user.py` | 成功 |
| 4 | 合法新建 `src/helper.py` | 成功 |
| 5 | 查看 Diff | 只出现 `src/user.py` 和 `src/helper.py` |
| 6 | 执行撤销 | `src/user.py` 恢复 Task 开始状态，`src/helper.py` 被删除 |
| 7 | 检查用户预先修改 | `# user change before agent task` 仍然存在 |

### 6.4 通过条件

- 保护测试文件哈希始终不变；
- Workspace 外没有创建 `outside.txt`；
- 非法 Patch 没有产生部分写入；
- 撤销后 Workspace 与 Task Snapshot 中受管理文件完全一致；
- Agent 新文件已删除；
- 用户 Task 前的修改仍然存在；
- 最终状态为 `reverted`；
- Trace 包含两次拒绝、合法修改和撤销结果。

---

## 7. E05：预算耗尽与继续

### 7.1 验收目的

确定性验证超过单次执行预算时不会丢失现场，并且用户继续后仍属于同一 Task。

### 7.2 测试设置

为测试加速，把当前 Run 预算临时配置为：

- 最大 Turn：3；
- 最大主动执行时长：60 秒。

`ScriptedModelAdapter` 前 3 个 Turn 只执行合法读取和一次未完成的 Patch，不运行成功 Validation。

### 7.3 操作与预期

1. Run 1 达到第 3 个 Turn；
2. 状态进入 `budget_exhausted`；
3. 页面展示当前修改、Diff、已用 Turn 和继续按钮；
4. 记录 Task ID、Run 1 ID、Snapshot 哈希和当前文件哈希；
5. 用户点击“继续当前任务”；
6. 创建不同的 Run 2 ID；
7. Task ID、初始 Snapshot、当前文件和历史 Trace 保持不变；
8. `ScriptedModelAdapter` 在 Run 2 完成 Patch 并运行成功 Validation；
9. 最终 Diff 仍然相对最初 Task Snapshot。

### 7.4 通过条件

- Run 1 没有因预算耗尽自动撤销；
- Run 1 最终状态为 `budget_exhausted`；
- Run 2 使用新预算；
- Task ID 未变化，Run ID 已变化；
- 最终状态为 `success`；
- Task Trace 可以关联两个 Run；
- 最终 Diff 不以 Run 2 开始状态为基准。

---

## 8. E06：用户停止

### 8.1 验收目的

确定性验证用户可以停止正在执行的工具或 Validation，且停止后保留现场。

### 8.2 测试方式

使用可阻塞的 FakeValidationRunner：

1. Agent 先完成一次合法 Patch；
2. 调用 `run_validation`；
3. FakeValidationRunner 发出“已启动”信号并保持运行；
4. 测试触发用户“停止任务”；
5. Runner 记录 terminate 请求并结束。

### 8.3 通过条件

- 页面在 `validating` 状态提供停止操作；
- 停止请求后 Runner 收到终止信号；
- 最终状态为 `cancelled`；
- 已产生的 Patch、当前 Diff 和 Trace 被保留；
- 页面提供继续、保留和撤销；
- 点击撤销后可以恢复 Snapshot；
- 不存在仍在运行的 Validation 进程。

---

## 9. 横向功能验收

| ID | 场景 | 操作 | 预期 |
| --- | --- | --- | --- |
| C01 | 无效 Workspace | 输入不存在目录 | 不创建 Task，展示明确错误 |
| C02 | 配置缺失 | 项目无配置文件 | 不允许分析 |
| C03 | 用户拒绝 | 分析后点击拒绝 | 状态 `rejected`，文件哈希不变 |
| C04 | 模型临时错误 | Adapter 连续返回服务错误 | 自动重试 2 次后进入可恢复 `failed` |
| C05 | 非法 Tool Call | 缺少必填参数 | 返回 `invalid_tool_call`，不执行工具 |
| C06 | 重复 Tool Call | 相同工具参数连续 3 次 | 第 3 次不执行，进入 `failed` |
| C07 | 外部并发修改 | Task 中途人工修改目标文件 | 拒绝覆盖，返回 `concurrent_modification` |
| C08 | Validation 超时 | Runner 超过配置时长 | 进入 `repairing`，错误标记 timeout |
| C09 | Validation 无法启动 | 命令不存在或进程错误 | 进入 `failed`，不误判为代码测试失败 |
| C10 | 模型声称完成但未测试 | 模型输出“已完成” | 不进入 Success，继续要求验证 |
| C11 | Patch 后旧测试曾通过 | 测试通过后再发生 Patch | Success 失效，必须重新测试 |
| C12 | Trace 脱敏 | 输出包含测试 Token | 模型上下文和 Trace 中只出现掩码 |
| C13 | Trace 写入失败 | 模拟磁盘写入错误 | 页面告警，不记为完整验收通过 |
| C14 | Diff | Task 前存在用户修改 | Diff 只计算 Agent 相对 Task Snapshot 的变化 |
| C15 | 新任务 | 保留修改后开始新任务 | 新 Task 使用当前 Workspace 作为新起点 |
| C16 | 分阶段停止 | 分别在 analyzing、executing、repairing 触发停止 | 均进入 `cancelled`，不再发起新的模型或工具调用 |
| C17 | 敏感路径 | 尝试列出或读取 `.env` 内容 | 返回 `path_sensitive`，模型上下文和 Trace 不含文件内容 |
| C18 | 只读测试 | 先读取、再尝试修改 `tests/test_user.py` | 读取成功，写入返回 `path_protected` |

---

## 10. 页面验收清单

### 10.1 等待任务

- 显示 Workspace 绝对路径；
- 显示固定验证命令；
- 显示可编辑和保护范围；
- 任务为空时“分析任务”不可用；
- Workspace 无效时说明具体原因。

### 10.2 等待批准

- 显示任务理解；
- 显示相关文件；
- 显示计划修改内容；
- 显示预计修改文件；
- 显示验证方式；
- 同时提供批准和拒绝；
- 批准前磁盘无 Agent 修改。

### 10.3 执行中

- 显示当前阶段；
- 显示已完成操作摘要；
- 不展示隐藏推理；
- 提供停止；
- 不允许重复点击批准创建多个 Run。

### 10.4 结果页

- 显示明确状态；
- 显示最后一次 Validation；
- 显示修改文件和 Unified Diff；
- 成功、失败、停止、预算耗尽使用不同文案；
- 只在最近一次 Validation 通过时显示“验证通过”；
- 按状态显示正确按钮；
- 撤销部分失败时列出具体文件。

---

## 11. Trace 验收清单

每个 Task 至少检查：

- Task ID 和每个 Run ID 存在；
- Task 和 Run 开始事件存在；
- 每次状态转换可按时间还原；
- 用户任务和批准/拒绝记录存在；
- 每个 Tool Call 都有对应 Tool Result，或明确标记未执行；
- 每次 Validation 有退出码、是否超时和耗时；
- 每个 Patch 后有 Diff 更新记录；
- 最终状态与页面一致；
- Token 数据在模型接口提供时存在；
- API Key、密码和敏感路径文件内容不存在；
- 没有模型隐藏推理；
- Trace JSONL 每行均可独立解析。

---

## 12. 外部通用模型试跑（可选、不阻塞 MVP 验收）

### 12.1 运行次数

- E01 运行 5 次；
- E02 运行 5 次；
- 每次使用新的 Fixture 副本、Task 和 Runtime Data；
- 使用同一模型版本、System Prompt、工具版本和推理参数；
- 中途不得人工修改代码；
- 用户批准计划不视为额外干预；
- 用户为解决模型问题补充说明时，必须记录一次人工介入。

本节用于形成方向判断数据。未配置外部模型时，不执行本节，不影响独立 Harness MVP 的开发完成判定。

### 12.2 记录字段

| 字段 | 说明 |
| --- | --- |
| 任务 | E01 或 E02 |
| 运行序号 | 1～5 |
| 模型和版本 | 实际模型 ID |
| Prompt 版本 | 内容哈希或版本号 |
| Harness 版本 | Git Commit 或构建版本 |
| 最终状态 | success/failed/budget_exhausted 等 |
| 任务是否通过 | 结合 pytest 和人工 Diff 判断 |
| 首次 Validation | 通过、失败或未执行 |
| 是否自主修复成功 | 是、否、不适用 |
| Turn | Task 总模型回合 |
| Tool Call | Task 总工具调用 |
| Validation 次数 | Task 总测试次数 |
| 总主动执行时长 | 不含等待用户时间 |
| Token | 输入、输出和总量 |
| 人工介入 | 次数和内容 |
| 主失败分类 | 第 2.2 节分类 |
| 备注 | 无关修改、异常行为等 |

### 12.3 结果汇总

至少计算：

- 端到端任务通过率；
- Validation 最终通过率；
- 首次失败后的自主修复成功率；
- 平均和中位 Turn；
- 平均和中位耗时；
- 平均 Token；
- 人工介入率；
- 越权尝试次数和是否被 Harness 阻止；
- 撤销成功率；
- 各失败分类数量。

### 12.4 方向判断

满足以下全部条件，建议进入 目标平台集成评估：

- 任务通过至少 7/10；
- 存在首次 Validation 失败的样本时，自主修复成功率不低于 50%；
- 成功样本中至少 80% 经人工检查无明显需求偏差或无关修改；
- 越权读取或修改成功次数为 0；
- 撤销失败和用户原有代码丢失次数为 0。

未满足时，不直接判断模型“不能做 Coding”。应先根据 Trace 输出：

- 模型问题；
- System Prompt 问题；
- Tool Schema 问题；
- 上下文拼装问题；
- Harness 状态或工具问题；
- 项目和测试设计问题。

---

## 13. 验收报告模板

```markdown
# Coding Agent Harness MVP 验收报告

## 1. 环境
- Harness 版本：
- 模型及版本：
- Prompt 版本：
- Tool Schema 版本：
- 日期：

## 2. 确定性验收
| 用例 | 结果 | 失败原因 |
| --- | --- | --- |
| E03 自主修复 | 通过/失败 | |
| E04 权限与撤销 | 通过/失败 | |
| E05 预算与继续 | 通过/失败 | |
| E06 用户停止 | 通过/失败 | |
| C01～C18 | 通过数/总数 | |

## 3. 外部通用模型试跑（如执行）
| 任务 | 序号 | 是否通过 | 是否自主修复 | Turn | 耗时 | Token | 失败分类 |
| --- | --- | --- | --- | --- | --- | --- | --- |

## 4. 指标汇总
- 任务通过率：
- 自主修复成功率：
- 人工检查合格率：
- 人工介入率：
- 越权成功次数：
- 撤销失败次数：

## 5. 主要问题
- 模型：
- Prompt：
- 工具：
- 上下文：
- Harness：
- 验收项目：

## 6. 结论
- 是否完成 MVP：
- 是否达到继续投入判断线：
- 是否建议接入目标模型：
- 下一轮优先事项：
```

---

## 14. MVP 验收完成定义

以下全部满足，才可以宣布 MVP 验收完成：

1. E01、E02 各至少由测试专用 DemoModelAdapter 端到端通过一次；
2. E03～E06 确定性验收全部通过；
3. C01～C18 全部通过；
4. 页面验收清单全部通过；
5. Trace 验收清单全部通过；
6. 没有越权写入、错误撤销或用户 Task 前代码丢失；
7. README 可以让另一名体验者独立启动应用并复现 E01 或 E02；
8. 形成完整验收报告。

外部模型试跑的 7/10 判断线用于决定后续方向，不作为“代码是否开发完成”的必要条件。目标模型和 目标平台的联调不在本验收范围。
