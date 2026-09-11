"""Streamlit entrypoint for the Coding Agent Harness MVP."""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
from pathlib import Path
from types import MethodType
from dataclasses import replace
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components

import agent_harness.helper_scripts as helper_scripts_module
import agent_harness.javascript as javascript_module
import agent_harness.orchestrator as orchestrator_module
import agent_harness.prompts as prompts_module
import agent_harness.tools as tools_module
import agent_harness.validation as validation_module
from agent_harness.config import WorkspaceConfig
from agent_harness.domain import ACTIVE_STATES, ExecutionMode, TaskState
from agent_harness.errors import AgentHarnessError
from agent_harness.model_adapter import OpenAICompatibleAdapter
from agent_harness.project_bootstrap import create_website_workspace
from agent_harness.website_output import (
    build_inline_preview,
    build_website_zip,
    website_exists,
)


# Streamlit reruns app.py in one process and can retain imported submodules.
# Reload the runtime dependency chain only when a preserved process still has
# the pre-v0.3.7 orchestrator class; the active Task object is upgraded below.
if not hasattr(orchestrator_module.TaskOrchestrator, "_auto_continue_run"):
    javascript_module = importlib.reload(javascript_module)
    helper_scripts_module = importlib.reload(helper_scripts_module)
    validation_module = importlib.reload(validation_module)
    prompts_module = importlib.reload(prompts_module)
    tools_module = importlib.reload(tools_module)
    orchestrator_module = importlib.reload(orchestrator_module)

TaskOrchestrator = orchestrator_module.TaskOrchestrator
ToolExecutor = tools_module.ToolExecutor
ValidationRunner = validation_module.ValidationRunner


STATE_LABELS = {
    "idle": "等待任务",
    "analyzing": "正在分析",
    "awaiting_approval": "等待批准",
    "executing": "正在修改",
    "validating": "正在验证",
    "repairing": "正在修复",
    "success": "验证通过",
    "cancelled": "已停止",
    "budget_exhausted": "达到执行上限",
    "failed": "执行失败",
    "rejected": "已拒绝",
    "reverted": "已撤销",
}

EXECUTION_AUTO_LABEL = "自动执行"
EXECUTION_CONFIRM_LABEL = "确认后执行"


def spawn_worker(agent: TaskOrchestrator) -> threading.Thread:
    worker = threading.Thread(
        target=agent.run_until_pause,
        name="harness-worker",
        daemon=True,
    )
    worker.start()
    st.session_state.worker = worker
    return worker


@st.cache_resource
def get_task_registry():
    """Keep active Task objects across browser refreshes in this server process."""
    return {"agents": {}, "lock": threading.RLock()}


def reset_task() -> None:
    current = st.session_state.get("agent")
    if current is not None:
        registry = get_task_registry()
        with registry["lock"]:
            registry["agents"].pop(current.task_id, None)
    st.query_params.clear()
    st.session_state.agent = None
    st.session_state.worker = None
    st.session_state.last_ui_error = None


def build_model():
    return OpenAICompatibleAdapter(
        base_url=st.session_state.model_base_url,
        api_key=st.session_state.model_api_key,
        model=st.session_state.model_id,
        temperature=st.session_state.model_temperature,
    )


def display_plan(plan) -> None:
    st.subheader("执行计划")
    st.write(plan["task_summary"])
    left, right = st.columns(2)
    with left:
        st.caption("相关文件")
        for item in plan["relevant_files"]:
            st.code(item, language=None)
    with right:
        st.caption("预计修改文件")
        for item in plan["expected_modified_files"]:
            st.code(item, language=None)
    st.caption("计划修改")
    for item in plan["planned_changes"]:
        st.markdown("- " + item)
    st.caption("验证方式")
    st.write(plan["validation_plan"])
    if plan["risks_or_questions"]:
        st.warning("\n".join(plan["risks_or_questions"]))


def format_duration(seconds: float) -> str:
    """Render active task time without exposing raw fractional seconds."""
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return "%s 小时 %02d 分" % (hours, minutes)
    if minutes:
        return "%s 分 %02d 秒" % (minutes, secs)
    return "%s 秒" % secs


def validation_status(validation, state: str) -> str:
    """Return the latest fixed-validation status for the primary header."""
    if state == TaskState.VALIDATING.value:
        return "验证中"
    if not validation:
        return "尚未验证"
    if validation["runner_error"]:
        return "运行异常"
    if validation["cancelled"]:
        return "已停止"
    if validation["timed_out"]:
        return "已超时"
    if validation["exit_code"] == 0:
        return "通过"
    return "未通过"


st.set_page_config(
    page_title="Coding Agent Harness MVP",
    page_icon="🧪",
    layout="wide",
)

for key, value in {
    "agent": None,
    "worker": None,
    "last_ui_error": None,
    "workspace_preview": None,
    "workspace_path": "",
    "task_text": "",
    "model_base_url": os.environ.get("AGENT_HARNESS_BASE_URL", ""),
    "model_api_key": os.environ.get("AGENT_HARNESS_API_KEY", ""),
    "model_id": os.environ.get("AGENT_HARNESS_MODEL", ""),
    "model_temperature": min(
        1.0,
        max(0.1, float(os.environ.get("AGENT_HARNESS_TEMPERATURE", "0.1"))),
    ),
    "model_probe": None,
    "execution_mode": EXECUTION_AUTO_LABEL,
    "project_mode": "从零创建网站",
}.items():
    if key not in st.session_state:
        st.session_state[key] = value

# Keep tasks and browser sessions created before the user-facing label cleanup.
st.session_state.execution_mode = {
    "自主模式（实验）": EXECUTION_AUTO_LABEL,
    "审批模式（基线）": EXECUTION_CONFIRM_LABEL,
}.get(st.session_state.execution_mode, st.session_state.execution_mode)

task_registry = get_task_registry()
requested_task_id = st.query_params.get("task")
if st.session_state.agent is None and requested_task_id:
    with task_registry["lock"]:
        recovered_agent = task_registry["agents"].get(requested_task_id)
    if recovered_agent is not None:
        st.session_state.agent = recovered_agent
        st.session_state.worker = None
        st.session_state.execution_mode = (
            EXECUTION_AUTO_LABEL
            if getattr(recovered_agent, "execution_mode", ExecutionMode.SUPERVISED)
            == ExecutionMode.AUTONOMOUS
            else EXECUTION_CONFIRM_LABEL
        )
        if hasattr(recovered_agent.model, "models_url"):
            st.session_state.model_base_url = recovered_agent.model.models_url[
                : -len("/models")
            ]
            st.session_state.model_id = recovered_agent.model.model
            st.session_state.model_temperature = recovered_agent.model.temperature
    else:
        st.query_params.clear()
        st.session_state.last_ui_error = (
            "无法恢复该任务：本地服务可能已经重启。运行记录仍保存在磁盘。"
        )

st.title("Coding Agent Harness")
st.caption("在本地完成代码读取、修改与验证")

agent: Optional[TaskOrchestrator] = st.session_state.agent

# Streamlit preserves Task objects across source reloads. Refresh the stateless
# executor while paused so a running exploration can pick up safe tool fixes
# without discarding its Task, Snapshot, model history, or API configuration.
if agent is not None and not agent.worker_active:
    if not hasattr(agent, "max_total_turns"):
        agent.max_total_turns = max(  # type: ignore[attr-defined]
            agent.total_turns + 80,
            agent.max_turns * 3,
        )
    if not hasattr(agent, "max_total_active_seconds"):
        agent.max_total_active_seconds = max(  # type: ignore[attr-defined]
            int(agent.total_active_seconds) + 60 * 60,
            agent.max_active_seconds * 2,
        )
    if not hasattr(agent, "max_total_tokens"):
        existing_usage = getattr(agent, "usage", {})
        existing_tokens = int(
            existing_usage.get("total_tokens")
            or (
                existing_usage.get("prompt_tokens", 0)
                + existing_usage.get("completion_tokens", 0)
            )
        )
        agent.max_total_tokens = existing_tokens + 2_000_000  # type: ignore[attr-defined]
    if not hasattr(agent, "analysis_calls_since_change"):
        agent.analysis_calls_since_change = 0  # type: ignore[attr-defined]
    if not hasattr(agent, "run_successful_mutations"):
        agent.run_successful_mutations = 0  # type: ignore[attr-defined]
    if not hasattr(agent, "budget_scope"):
        agent.budget_scope = None  # type: ignore[attr-defined]
    if not hasattr(agent, "budget_reason"):
        agent.budget_reason = None  # type: ignore[attr-defined]

    # A paused exploration may span a Harness source update. Refresh the
    # context projection methods without replacing the durable Task object.
    agent._total_active_seconds_value = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._total_active_seconds_value,
        agent,
    )
    agent._total_token_count = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._total_token_count,
        agent,
    )
    agent._total_budget_reason = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._total_budget_reason,
        agent,
    )
    agent._workspace_checkpoint = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._workspace_checkpoint,
        agent,
    )
    agent._bound_tool_message = TaskOrchestrator._bound_tool_message  # type: ignore[attr-defined]
    agent._context_history = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._context_history,
        agent,
    )
    agent._prefers_exact_edit = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._prefers_exact_edit,
        agent,
    )
    agent._action_guard_message = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._action_guard_message,
        agent,
    )
    agent._update_action_progress = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._update_action_progress,
        agent,
    )
    agent._build_messages = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._build_messages,
        agent,
    )
    agent._allowed_tools = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._allowed_tools,
        agent,
    )
    agent._available_tool_schemas = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._available_tool_schemas,
        agent,
    )
    agent._repeated_tool_error = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._repeated_tool_error,
        agent,
    )
    agent._execute_tool = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._execute_tool,
        agent,
    )
    agent._budget_exhausted = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._budget_exhausted,
        agent,
    )
    agent._run_budget_reason = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._run_budget_reason,
        agent,
    )
    agent._can_auto_continue_batch = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._can_auto_continue_batch,
        agent,
    )
    agent._auto_continue_run = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._auto_continue_run,
        agent,
    )
    agent._enter_budget_exhausted = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._enter_budget_exhausted,
        agent,
    )
    agent.run_until_pause = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator.run_until_pause,
        agent,
    )
    if not hasattr(agent, "run_usage"):
        agent.run_usage = {}  # type: ignore[attr-defined]
    agent._update_usage = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._update_usage,
        agent,
    )
    agent._finish_run = MethodType(  # type: ignore[attr-defined]
        TaskOrchestrator._finish_run,
        agent,
    )
    agent.continue_task = MethodType(TaskOrchestrator.continue_task, agent)
    agent.get_view = MethodType(TaskOrchestrator.get_view, agent)
    runtime_config = agent.config
    if runtime_config.validation_command[0] in {"python", "python3"}:
        runtime_config = replace(
            runtime_config,
            validation_command=(
                sys.executable,
                *runtime_config.validation_command[1:],
            ),
        )
    agent.config = runtime_config
    agent.snapshot.config = runtime_config
    agent.validation_runner = ValidationRunner(
        runtime_config,
        agent.redactor,
        sandboxed=(
            getattr(agent, "execution_mode", ExecutionMode.SUPERVISED)
            == ExecutionMode.AUTONOMOUS
        ),
        scratch_dir=(
            agent.task_dir / "validation_tmp"
            if getattr(agent, "execution_mode", ExecutionMode.SUPERVISED)
            == ExecutionMode.AUTONOMOUS
            else None
        ),
    )
    agent.tools = ToolExecutor(
        runtime_config,
        agent.snapshot,
        agent.validation_runner,
        getattr(agent, "helper_dir", None)
        if getattr(agent, "execution_mode", ExecutionMode.SUPERVISED)
        == ExecutionMode.AUTONOMOUS
        else None,
    )

with st.sidebar:
    if agent is None:
        st.header("任务设置")
        execution_mode_label = st.selectbox(
            "执行方式",
            [EXECUTION_AUTO_LABEL, EXECUTION_CONFIRM_LABEL],
            key="execution_mode",
            help="自动执行会连续完成任务；确认后执行会先展示计划。",
        )
        st.subheader("模型 API")
        st.caption("接口需要支持 OpenAI-compatible Tool Calling。")
        st.caption(
            "MiniMax M3 预设的接口地址取自环境变量 `AGENT_HARNESS_PRESET_BASE_URL`。"
        )
        if st.button("使用 MiniMax M3 预设", use_container_width=True):
            st.session_state.model_base_url = os.environ.get(
                "AGENT_HARNESS_PRESET_BASE_URL", ""
            )
            st.session_state.model_id = "MiniMax-M3"
            st.session_state.model_temperature = 1.0
            st.session_state.model_probe = None
            st.rerun()
        st.text_input(
            "接口地址",
            key="model_base_url",
            placeholder="https://example.com/v1",
        )
        st.text_input("模型 ID", key="model_id")
        st.text_input(
            "API Key",
            key="model_api_key",
            type="password",
        )
        with st.expander("高级设置", expanded=False):
            st.slider(
                "生成随机性",
                min_value=0.1,
                max_value=1.0,
                step=0.1,
                key="model_temperature",
                help="对应模型接口的 temperature 参数。",
            )
        probe_disabled = not all(
            [
                st.session_state.model_base_url.strip(),
                st.session_state.model_id.strip(),
                st.session_state.model_api_key.strip(),
            ]
        )
        if st.button(
            "检测模型连接",
            disabled=probe_disabled,
            use_container_width=True,
        ):
            try:
                probe_model = build_model()
                model_ids = probe_model.list_models()
                selected = st.session_state.model_id.strip()
                matching = [
                    item
                    for item in model_ids
                    if "minimax" in item.lower() or "m3" in item.lower()
                ]
                st.session_state.model_probe = {
                    "ok": selected in model_ids,
                    "selected": selected,
                    "models": model_ids,
                    "matching": matching,
                }
            except Exception as exc:
                st.session_state.model_probe = {
                    "ok": False,
                    "error": str(exc),
                }

        probe = st.session_state.model_probe
        if probe:
            if probe.get("error"):
                st.error(probe["error"])
            elif probe["ok"]:
                st.success("连接成功，当前模型 ID 可用")
            else:
                candidates = probe.get("matching") or probe.get("models", [])[:20]
                st.warning(
                    "连接成功，但列表中未找到 %s。可用候选：%s"
                    % (probe["selected"], "、".join(candidates) or "未返回")
                )
        if execution_mode_label == EXECUTION_AUTO_LABEL:
            st.info(
                "自动执行会在工作副本中连续完成任务，不会修改源项目。"
            )
            with st.expander("安全说明", expanded=False):
                st.caption(
                    "模型可以修改工作副本中的未保护代码。临时分析工具不能写入工程、"
                    "联网或启动子进程，最终验证也在沙箱中执行。"
                )
        else:
            st.info(
                "确认后执行会先展示计划，得到确认后再修改指定项目。"
            )
            with st.expander("使用说明", expanded=False):
                st.caption(
                    "该方式直接修改指定项目，只对可信项目使用，并避免执行期间"
                    "同时编辑同一文件。"
                )
    else:
        execution_mode_label = st.session_state.execution_mode
        st.header("当前任务")
        st.caption("执行方式")
        st.write(execution_mode_label)
        st.caption("模型")
        st.write(getattr(agent.model, "model", getattr(agent.model, "name", "未知模型")))
        st.caption("开始新任务后可以重新配置。")

if agent is None:
    st.subheader("1. 选择项目来源")
    project_mode = st.radio(
        "项目来源",
        ["从零创建网站", "使用已有代码项目"],
        key="project_mode",
        horizontal=True,
        label_visibility="collapsed",
    )
    if project_mode == "从零创建网站":
        st.success(
            "无需准备代码目录。系统会根据任务自动创建空白项目、"
            "权限配置和独立基础验收。"
        )
        st.caption(
            "当前一键模板面向原生 HTML/CSS/JavaScript 静态网站；"
            "自动验收通过后仍需人工检查视觉和真实可玩性。"
            "长任务在有效推进时会自动续跑，并受时间和用量上限保护。"
        )
    else:
        workspace_col, validate_col = st.columns([4, 1])
        with workspace_col:
            st.text_input(
                "工作区路径",
                key="workspace_path",
                placeholder="/absolute/path/to/code-project",
                label_visibility="collapsed",
            )
        with validate_col:
            validate_workspace = st.button("校验工作区", use_container_width=True)

        if validate_workspace:
            try:
                preview = WorkspaceConfig.load(st.session_state.workspace_path)
                st.session_state.workspace_preview = preview.public_summary()
                st.session_state.last_ui_error = None
            except (AgentHarnessError, ValueError) as exc:
                st.session_state.workspace_preview = None
                st.session_state.last_ui_error = str(exc)

        preview = st.session_state.workspace_preview
        if preview:
            st.success("工作区配置有效")
            with st.expander("查看工作区规则", expanded=False):
                summary_cols = st.columns(3)
                summary_cols[0].metric(
                    "可编辑范围",
                    ", ".join(preview["editable_paths"]),
                )
                summary_cols[1].metric(
                    "只读范围",
                    ", ".join(preview["protected_paths"]) or "无",
                )
                summary_cols[2].metric(
                    "验证超时",
                    "%s 秒" % preview["validation_timeout_seconds"],
                )
                st.code(" ".join(preview["validation_command"]), language="bash")
                if execution_mode_label == EXECUTION_AUTO_LABEL:
                    st.caption(
                        "实际执行时会创建工作副本，并开放除只读保护和敏感路径外的工程文件。"
                    )

    st.subheader("2. 描述想要完成的任务")
    st.text_area(
        "描述代码问题或要实现的功能",
        key="task_text",
        height=180,
        placeholder=(
            "例如：写一个小游戏网站，里面需要有 20 个童年游戏，"
            "其中一个是可在网页中直接玩的简化版我的世界。"
            if project_mode == "从零创建网站"
            else "例如：用户注册时，空用户名没有返回错误。请定位并修复。"
        ),
    )
    creating_website = project_mode == "从零创建网站"
    start_disabled = (
        not st.session_state.task_text.strip()
        or (
            not creating_website
            and not st.session_state.workspace_path.strip()
        )
        or not all(
            [
                st.session_state.model_base_url.strip(),
                st.session_state.model_id.strip(),
                st.session_state.model_api_key.strip(),
            ]
        )
    )
    if st.button(
        (
            "开始创建网站"
            if creating_website
            else "开始任务"
        ),
        type="primary",
        disabled=start_disabled,
        use_container_width=True,
    ):
        try:
            model = build_model()
            selected_workspace = (
                str(create_website_workspace(st.session_state.task_text))
                if creating_website
                else st.session_state.workspace_path
            )
            agent = TaskOrchestrator(
                workspace=selected_workspace,
                task_text=st.session_state.task_text,
                model=model,
                max_turns=40 if creating_website else 20,
                max_active_seconds=30 * 60 if creating_website else 15 * 60,
                max_total_turns=120 if creating_website else 60,
                max_total_active_seconds=(
                    60 * 60 if creating_website else 30 * 60
                ),
                max_total_tokens=2_000_000 if creating_website else 800_000,
                model_timeout_seconds=120 if creating_website else 60,
                execution_mode=(
                    ExecutionMode.AUTONOMOUS
                    if execution_mode_label == EXECUTION_AUTO_LABEL
                    else ExecutionMode.SUPERVISED
                ),
            )
            st.session_state.agent = agent
            with task_registry["lock"]:
                task_registry["agents"][agent.task_id] = agent
            st.query_params["task"] = agent.task_id
            st.session_state.last_ui_error = None
            spawn_worker(agent)
            st.rerun()
        except Exception as exc:
            st.session_state.last_ui_error = str(exc)

    if st.session_state.last_ui_error:
        st.error(st.session_state.last_ui_error)
else:
    view = agent.get_view()
    state = view["state"]
    label = STATE_LABELS.get(state, state)
    if (
        view.get("execution_mode") == ExecutionMode.AUTONOMOUS.value
        and state == TaskState.EXECUTING.value
    ):
        label = "正在执行"

    validation = view["last_validation"]
    header_status, header_duration, header_validation = st.columns([3, 1, 1])
    header_status.subheader(label)
    header_duration.metric(
        "执行时长",
        format_duration(view["total_active_seconds"]),
        help="仅统计模型和系统主动执行时间。",
    )
    header_validation.metric(
        "验证结果",
        validation_status(validation, state),
        help="以最近一次固定验证为准；详细输出见页面下方。",
    )

    with st.expander("运行详情", expanded=False):
        st.caption("模型用量与任务追踪信息")
        detail_turn, detail_tools, detail_helpers, detail_tokens = st.columns(4)
        detail_turn.metric(
            "模型轮次",
            "%s / %s" % (view["total_turns"], view["max_total_turns"]),
            help="当前批次：%s / %s" % (view["run_turns"], view["max_turns"]),
        )
        detail_tools.metric(
            "工具调用",
            view["total_tool_calls"],
            help="模型发出且由系统处理的工具调用累计次数。",
        )
        detail_helpers.metric(
            "辅助分析",
            view.get("total_helper_runs", 0),
            help=(
                "只统计脚本运行次数；当前批次：%s / %s。"
                % (view.get("run_helper_runs", 0), view.get("max_helper_runs", 0))
            ),
        )
        total_tokens = view.get("total_tokens", 0)
        max_total_tokens = view.get("max_total_tokens")
        detail_tokens.metric(
            "Token 用量",
            (
                "%s / %s" % (f"{total_tokens:,}", f"{max_total_tokens:,}")
                if max_total_tokens is not None
                else f"{total_tokens:,}"
            ),
        )
        detail_model, detail_ids = st.columns(2)
        with detail_model:
            st.caption("模型")
            st.code(view.get("model", "未知"), language=None)
        with detail_ids:
            st.caption("任务 ID / 批次 ID")
            st.code(
                "%s\n%s" % (view["task_id"], view["run_id"]),
                language=None,
            )
        st.caption("运行记录文件")
        st.code(view["trace_path"], language=None)

    with st.expander("工作区与任务", expanded=False):
        if view.get("workspace_isolated"):
            st.caption("源项目，不会修改")
            st.code(view.get("source_workspace", ""), language=None)
            st.caption("工作副本")
        st.code(view["workspace"]["root"], language=None)
        st.write(view["task"])
        st.caption("验证命令")
        st.code(" ".join(view["workspace"]["validation_command"]), language="bash")

    st.subheader("执行记录")
    event_box = st.container(height=220)
    with event_box:
        for event in view["events"]:
            st.markdown(
                "`%s` · %s" % (event["time"], event["text"])
            )

    if state in {item.value for item in ACTIVE_STATES}:
        if st.button("停止任务", type="secondary"):
            agent.request_stop()
            st.rerun()

    if view["plan"]:
        display_plan(view["plan"])

    if state == TaskState.AWAITING_APPROVAL.value:
        approve_col, reject_col = st.columns(2)
        if approve_col.button("确认并执行", type="primary", use_container_width=True):
            agent.approve_plan()
            spawn_worker(agent)
            st.rerun()
        if reject_col.button("取消任务", use_container_width=True):
            agent.reject_plan()
            st.rerun()

    if validation:
        st.subheader("最近一次验证")
        if validation["runner_error"]:
            st.error(validation["runner_error"])
        elif validation["cancelled"]:
            st.warning("验证已由用户停止")
        elif validation["timed_out"]:
            st.warning("验证超时")
        elif validation["exit_code"] == 0:
            st.success("项目验证通过")
        else:
            st.error("项目验证未通过，退出码为 %s" % validation["exit_code"])
        output = "\n".join(
            part
            for part in [validation["stdout"], validation["stderr"]]
            if part
        )
        if output:
            st.code(output, language=None)

    if view["changed_files"] or state in {
        TaskState.SUCCESS.value,
        TaskState.CANCELLED.value,
        TaskState.BUDGET_EXHAUSTED.value,
        TaskState.FAILED.value,
    }:
        st.subheader("结果与代码变更")
        if view["changed_files"]:
            st.write("修改文件：" + "、".join(view["changed_files"]))
        else:
            st.write("当前没有代码变更")
        with st.expander("查看代码差异", expanded=state == TaskState.SUCCESS.value):
            st.code(view["diff"] or "无代码变更", language="diff")

    website_root = Path(view["workspace"]["root"])
    if not view["worker_active"] and website_exists(website_root):
        st.subheader("网站产物")
        st.info(
            "多游戏网站的自动验收会逐个启动游戏并拦截同步报错或空白舞台。"
            "操作手感、完整玩法和视觉效果仍需你在预览中检查。"
        )
        output_left, output_right = st.columns(2)
        show_preview = output_left.toggle(
            "加载网页预览",
            key="website-preview-%s" % view["task_id"],
            help="模型生成的 HTML/CSS/JavaScript 将在隔离 iframe 中运行。",
        )
        try:
            website_zip = build_website_zip(agent.config)
            output_right.download_button(
                "下载网站 ZIP",
                data=website_zip,
                file_name="harness-generated-website.zip",
                mime="application/zip",
                use_container_width=True,
            )
        except Exception as exc:
            output_right.warning("暂时无法导出：%s" % exc)
        if show_preview:
            try:
                components.html(
                    build_inline_preview(website_root),
                    height=760,
                    scrolling=True,
                )
            except Exception as exc:
                st.error("无法加载网页预览：%s" % exc)

    if view["last_error"]:
        st.error(
            "%s：%s"
            % (
                view["last_error"]["code"],
                view["last_error"]["message"],
            )
        )
        if view["last_error"].get("details"):
            with st.expander("查看脱敏错误详情"):
                st.json(view["last_error"]["details"])

    if state in {
        TaskState.CANCELLED.value,
        TaskState.BUDGET_EXHAUSTED.value,
        TaskState.FAILED.value,
    } and not view["finalized"]:
        if state == TaskState.BUDGET_EXHAUSTED.value:
            if view.get("budget_scope") == "task":
                st.info(
                    "任务总预算已用完，已有文件和运行记录仍保留。"
                    "请保留结果或新建任务，避免无上限空转。"
                )
            else:
                st.info(
                    "当前执行批次未能自动续跑，已有文件和历史仍保留。"
                    "继续会创建新批次，不会从零重做。"
                )
        note = st.text_area(
            "补充说明",
            key="continue_note",
            placeholder="例如：请重点检查边界值，不要修改测试。",
            help="没有补充内容时可以直接继续。",
        )
        action_cols = st.columns(3)
        can_continue = (
            (state != TaskState.FAILED.value or view["failure_recoverable"])
            and not (
                state == TaskState.BUDGET_EXHAUSTED.value
                and view.get("budget_scope") == "task"
            )
        )
        if action_cols[0].button(
            "继续当前任务" if state != TaskState.FAILED.value else "重试当前任务",
            disabled=not can_continue,
            use_container_width=True,
        ):
            should_run = agent.continue_task(note)
            if should_run:
                spawn_worker(agent)
            st.rerun()
        if action_cols[1].button("保留并结束", use_container_width=True):
            agent.keep_and_finish()
            st.rerun()
        if action_cols[2].button("撤销修改", use_container_width=True):
            agent.revert_task()
            st.rerun()

    if state == TaskState.SUCCESS.value and not view["finalized"]:
        st.success("验证通过。请仍然检查代码差异是否符合业务预期。")

    bottom_cols = st.columns(2)
    if bottom_cols[0].button(
        "撤销本次修改",
        disabled=not bool(view["changed_files"]) or view["worker_active"],
        use_container_width=True,
    ):
        agent.revert_task()
        st.rerun()
    if bottom_cols[1].button(
        "开始新任务",
        disabled=view["worker_active"],
        use_container_width=True,
    ):
        reset_task()
        st.rerun()
    worker = st.session_state.worker
    if worker is not None and worker.is_alive():
        time.sleep(0.6)
        st.rerun()
