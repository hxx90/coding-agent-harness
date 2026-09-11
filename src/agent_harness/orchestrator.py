"""Task orchestration, Agent Loop, state transitions, and user controls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import WorkspaceConfig, is_relative_to
from .domain import (
    ACTIVE_STATES,
    ExecutionMode,
    ModelResponse,
    NormalizedToolCall,
    Plan,
    TaskState,
    ToolResult,
    ValidationResult,
)
from .errors import ModelAPIError, AgentHarnessError, WorkspaceError
from .javascript import check_javascript_syntax
from .model_adapter import ModelAdapter
from .prompts import (
    BASE_PROMPT_VERSION,
    BASE_SYSTEM_PROMPT,
    POLICY_PROMPT_VERSION,
    allowed_tools_for_state,
    build_policy_prompt,
    tool_result_followup,
)
from .snapshot import SnapshotManager
from .tools import (
    ANALYZE_ALLOWED,
    AUTONOMOUS_ALLOWED,
    EXECUTE_ALLOWED,
    HELPER_TOOLS,
    LEGACY_HISTORY_MARKER,
    READ_TOOLS,
    TOOL_SCHEMAS,
    WRITE_TOOLS,
    ToolExecutor,
    infer_registered_game_ids,
)
from .trace import Redactor, TraceWriter
from .validation import ValidationRunner
from .workspace import create_isolated_workspace


TERMINAL_RUN_STATES = {
    TaskState.SUCCESS,
    TaskState.CANCELLED,
    TaskState.BUDGET_EXHAUSTED,
    TaskState.FAILED,
    TaskState.REJECTED,
    TaskState.REVERTED,
}

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
UNCLOSED_THINK_RE = re.compile(r"<think>.*\Z", re.IGNORECASE | re.DOTALL)
HISTORY_TAIL_ASSISTANT_TURNS = 6
MODEL_TOOL_TEXT_MAX_CHARS = 8_000


def trace_visible_text(value: Optional[str]) -> Optional[str]:
    """Remove provider reasoning blocks while preserving user-visible text."""
    if value is None:
        return None
    return UNCLOSED_THINK_RE.sub("", THINK_BLOCK_RE.sub("", value)).strip()


def trace_safe_messages(messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copy model messages without provider-private reasoning metadata."""
    safe: List[Dict[str, Any]] = []
    for original in messages:
        message = {
            key: value
            for key, value in original.items()
            if key not in {"reasoning_details", "reasoning_content"}
        }
        if message.get("role") == "assistant" and isinstance(
            message.get("content"), str
        ):
            message["content"] = trace_visible_text(message["content"])
        safe.append(message)
    return safe


def default_data_dir() -> Path:
    configured = os.environ.get("AGENT_HARNESS_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".agent-harness").resolve()


class TaskOrchestrator:
    """A single Task. Safe for one background worker and UI reads."""

    def __init__(
        self,
        *,
        workspace: str,
        task_text: str,
        model: ModelAdapter,
        data_dir: Optional[Path] = None,
        max_turns: int = 20,
        max_active_seconds: int = 15 * 60,
        max_total_turns: Optional[int] = None,
        max_total_active_seconds: Optional[int] = None,
        max_total_tokens: Optional[int] = None,
        model_timeout_seconds: int = 60,
        execution_mode: ExecutionMode = ExecutionMode.SUPERVISED,
        max_helper_runs: int = 10,
    ) -> None:
        if not isinstance(task_text, str) or not task_text.strip():
            raise ValueError("任务不能为空")
        if max_turns < 1 or max_active_seconds < 1 or max_helper_runs < 1:
            raise ValueError("执行预算必须大于 0")
        resolved_total_turns = (
            max_turns * 3 if max_total_turns is None else max_total_turns
        )
        resolved_total_seconds = (
            max_active_seconds * 2
            if max_total_active_seconds is None
            else max_total_active_seconds
        )
        if resolved_total_turns < max_turns:
            raise ValueError("任务总 Turn 上限不能小于单批次上限")
        if resolved_total_seconds < max_active_seconds:
            raise ValueError("任务总时长上限不能小于单批次上限")
        if max_total_tokens is not None and max_total_tokens < 1:
            raise ValueError("任务 Token 上限必须大于 0")

        self._lock = threading.RLock()
        self.stop_event = threading.Event()
        self.execution_mode = ExecutionMode(execution_mode)
        source_config = WorkspaceConfig.load(workspace)
        self.source_workspace = source_config.root
        self.workspace_isolated = self.execution_mode == ExecutionMode.AUTONOMOUS
        if self.workspace_isolated:
            isolated_root = create_isolated_workspace(source_config)
            self.config = WorkspaceConfig.load(str(isolated_root)).for_autonomous_copy()
        else:
            self.config = source_config
        self.model = model
        self.task_text = task_text.strip()
        self.task_id = str(uuid.uuid4())
        self.run_id = str(uuid.uuid4())
        self.state = TaskState.IDLE
        self.resume_state = (
            TaskState.EXECUTING
            if self.execution_mode == ExecutionMode.AUTONOMOUS
            else TaskState.ANALYZING
        )
        self.max_turns = max_turns
        self.max_active_seconds = max_active_seconds
        self.max_total_turns = resolved_total_turns
        self.max_total_active_seconds = resolved_total_seconds
        self.max_total_tokens = max_total_tokens
        self.model_timeout_seconds = model_timeout_seconds
        self.max_helper_runs = max_helper_runs
        self.run_helper_runs = 0
        self.total_helper_runs = 0
        self.run_turns = 0
        self.total_turns = 0
        self.run_tool_calls = 0
        self.total_tool_calls = 0
        self.run_active_seconds = 0.0
        self.total_active_seconds = 0.0
        self._active_segment_started: Optional[float] = None
        self._worker_active = False
        self._run_finished = False
        self._finalized = False
        self.plan: Optional[Plan] = None
        self.user_notes: List[str] = []
        self.last_validation: Optional[ValidationResult] = None
        self.last_error: Optional[Dict[str, Any]] = None
        self.failure_recoverable = False
        self.dirty_since_validation = False
        self.history: List[Dict[str, Any]] = [
            {"role": "user", "content": self.task_text}
        ]
        self.event_summaries: List[Dict[str, Any]] = []
        self._last_tool_signature: Optional[str] = None
        self._repeat_tool_count = 0
        self._tool_error_counts: Dict[str, int] = {}
        self._no_tool_streak = 0
        self.analysis_calls_since_change = 0
        self.run_successful_mutations = 0
        self.budget_scope: Optional[str] = None
        self.budget_reason: Optional[str] = None
        self.run_usage: Dict[str, int] = {}
        self.usage: Dict[str, int] = {}

        runtime_root = (data_dir or default_data_dir()).expanduser().resolve()
        if is_relative_to(runtime_root, self.config.root) or is_relative_to(
            runtime_root,
            self.source_workspace,
        ):
            raise WorkspaceError(
                "invalid_runtime_dir",
                "运行数据目录不能位于代码项目内",
                recoverable=True,
            )
        runtime_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(runtime_root, 0o700)
        except OSError:
            pass
        self.task_dir = runtime_root / "tasks" / self.task_id
        self.task_dir.mkdir(parents=True, exist_ok=False)

        self.redactor = Redactor()
        self.snapshot = SnapshotManager(self.config, self.task_dir)
        self.snapshot.create()
        self.validation_runner = ValidationRunner(
            self.config,
            self.redactor,
            sandboxed=self.execution_mode == ExecutionMode.AUTONOMOUS,
            scratch_dir=(
                self.task_dir / "validation_tmp"
                if self.execution_mode == ExecutionMode.AUTONOMOUS
                else None
            ),
        )
        self.helper_dir = self.task_dir / "helper_scripts"
        self.tools = ToolExecutor(
            self.config,
            self.snapshot,
            self.validation_runner,
            self.helper_dir
            if self.execution_mode == ExecutionMode.AUTONOMOUS
            else None,
        )
        self.trace = self._new_trace_writer(self.run_id)
        self._write_task_metadata()

        initial_state = (
            TaskState.EXECUTING
            if self.execution_mode == ExecutionMode.AUTONOMOUS
            else TaskState.ANALYZING
        )
        self._transition(initial_state, "task_submitted")
        self.trace.event(
            "task_created",
            self.state.value,
            {
                "task": self.task_text,
                "workspace": self.config.public_summary(),
                "model": self.model.name,
                "execution_mode": self.execution_mode.value,
                "workspace_isolated": self.workspace_isolated,
                "source_workspace": str(self.source_workspace),
                "prompt_versions": {
                    "base": BASE_PROMPT_VERSION,
                    "policy": POLICY_PROMPT_VERSION,
                    "base_sha256": hashlib.sha256(
                        BASE_SYSTEM_PROMPT.encode("utf-8")
                    ).hexdigest(),
                },
            },
        )
        self.trace.event(
            "run_started",
            self.state.value,
            {
                "max_turns": self.max_turns,
                "max_active_seconds": self.max_active_seconds,
                "max_total_turns": self.max_total_turns,
                "max_total_active_seconds": self.max_total_active_seconds,
                "max_total_tokens": self.max_total_tokens,
                "max_helper_runs": self.max_helper_runs,
            },
        )
        if self.execution_mode == ExecutionMode.AUTONOMOUS:
            self._add_summary("自主任务已创建，将在隔离副本中连续执行", "task")
        else:
            self._add_summary("任务已创建，开始只读分析", "task")

    def _new_trace_writer(self, run_id: str) -> TraceWriter:
        path = self.task_dir / "runs" / run_id / "trace.jsonl"
        return TraceWriter(
            path,
            task_id=self.task_id,
            run_id=run_id,
            redactor=self.redactor,
        )

    def _write_task_metadata(self) -> None:
        payload = {
            "task_id": self.task_id,
            "workspace": str(self.config.root),
            "task": self.task_text,
            "model": self.model.name,
            "execution_mode": self.execution_mode.value,
            "source_workspace": str(self.source_workspace),
            "workspace_isolated": self.workspace_isolated,
            "created_run_id": self.run_id,
            "budgets": {
                "max_turns_per_batch": self.max_turns,
                "max_active_seconds_per_batch": self.max_active_seconds,
                "max_total_turns": self.max_total_turns,
                "max_total_active_seconds": self.max_total_active_seconds,
                "max_total_tokens": self.max_total_tokens,
            },
        }
        (self.task_dir / "task.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _add_summary(self, text: str, kind: str = "info") -> None:
        self.event_summaries.append(
            {
                "time": time.strftime("%H:%M:%S"),
                "kind": kind,
                "text": text,
            }
        )
        if len(self.event_summaries) > 200:
            self.event_summaries = self.event_summaries[-200:]

    def _transition(self, target: TaskState, event: str, **data: Any) -> None:
        previous = self.state
        self.state = target
        if hasattr(self, "trace"):
            self.trace.event(
                "state_changed",
                target.value,
                {
                    "from": previous.value,
                    "to": target.value,
                    "trigger": event,
                    **data,
                },
            )

    def _active_seconds(self) -> float:
        value = self.run_active_seconds
        if self._active_segment_started is not None:
            value += time.monotonic() - self._active_segment_started
        return value

    def _total_active_seconds_value(self) -> float:
        value = self.total_active_seconds
        if self._active_segment_started is not None:
            value += time.monotonic() - self._active_segment_started
        return value

    def _total_token_count(self) -> int:
        if isinstance(self.usage.get("total_tokens"), int):
            return self.usage["total_tokens"]
        prompt = self.usage.get("prompt_tokens", self.usage.get("input_tokens", 0))
        completion = self.usage.get(
            "completion_tokens",
            self.usage.get("output_tokens", 0),
        )
        return int(prompt or 0) + int(completion or 0)

    def _total_budget_reason(self) -> Optional[str]:
        if self.total_turns >= self.max_total_turns:
            return "turns"
        if self._total_active_seconds_value() >= self.max_total_active_seconds:
            return "active_seconds"
        if (
            self.max_total_tokens is not None
            and self._total_token_count() >= self.max_total_tokens
        ):
            return "tokens"
        return None

    def _remaining_seconds(self) -> int:
        return max(0, int(self.max_active_seconds - self._active_seconds()))

    def _begin_active_segment(self) -> None:
        if self._active_segment_started is None:
            self._active_segment_started = time.monotonic()

    def _end_active_segment(self) -> None:
        if self._active_segment_started is not None:
            elapsed = time.monotonic() - self._active_segment_started
            self.run_active_seconds += elapsed
            self.total_active_seconds += elapsed
            self._active_segment_started = None

    def _finish_run(self, finish_reason: Optional[str] = None) -> None:
        if self._run_finished:
            return
        self._end_active_segment()
        self._run_finished = True
        self.trace.event(
            "run_finished",
            self.state.value,
            {
                "turns": self.run_turns,
                "tool_calls": self.run_tool_calls,
                "helper_script_runs": self.run_helper_runs,
                "active_seconds": round(self.run_active_seconds, 3),
                "usage": self.run_usage,
                "total_usage": self.usage,
                "error": self.last_error,
                "finish_reason": finish_reason,
            },
        )
        if self.state == TaskState.SUCCESS:
            diff = self.snapshot.diff()
            (self.task_dir / "final-diff.patch").write_text(diff, encoding="utf-8")
            self.trace.event(
                "task_finished",
                self.state.value,
                {
                    "final_state": self.state.value,
                    "changed_files": self.snapshot.changed_files(),
                    "diff": diff,
                },
            )
            self._finalized = True

    def _update_usage(self, usage: Optional[Dict[str, Any]]) -> None:
        if not usage:
            return
        for key, value in usage.items():
            if isinstance(value, int) and not isinstance(value, bool):
                self.run_usage[key] = self.run_usage.get(key, 0) + value
                self.usage[key] = self.usage.get(key, 0) + value

    def _default_work_state(self) -> TaskState:
        if self.execution_mode == ExecutionMode.AUTONOMOUS:
            if self.last_validation is not None and not self.last_validation.passed:
                return TaskState.REPAIRING
            return TaskState.EXECUTING
        return TaskState.REPAIRING if self.plan else TaskState.ANALYZING

    def _workspace_checkpoint(self) -> str:
        """Build a deterministic, code-free handoff for pruned history."""
        details = [
            "较早的已完成工具交互已从模型上下文中移除，完整记录仍在 Trace。",
            "当前工作区文件是唯一代码事实来源；不要重建被移除的工具参数，必要时重新 read_file。",
        ]
        app_path = self.config.root / "app.js"
        manifest_path = self.config.root / "game-manifest.json"
        if not app_path.is_file():
            return "[Harness Context Checkpoint]\n" + "\n".join(details)
        try:
            source = app_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return "[Harness Context Checkpoint]\n" + "\n".join(details)

        syntax = check_javascript_syntax(app_path, self.config.root)
        if syntax.get("available") and not syntax.get("passed"):
            diagnostic = str(syntax.get("diagnostics") or "").strip()
            if len(diagnostic) > 1600:
                diagnostic = diagnostic[:1600] + "\n...[诊断已截断]"
            details.append(
                "当前 app.js 未通过 JavaScript 语法检查，必须先修复再做其他分析或最终验证：\n"
                + diagnostic
            )

        leaked = source.count(LEGACY_HISTORY_MARKER)
        if leaked:
            details.append(
                "检测到 app.js 中有 %s 行旧版历史摘要被误写为源码；先删除这些完整行。"
                % leaked
            )
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                manifest = None
            if isinstance(manifest, list):
                ids = [
                    str(item.get("id"))
                    for item in manifest
                    if isinstance(item, dict) and item.get("id")
                ]
                if ids:
                    registered = infer_registered_game_ids(source, ids)
                    missing = [game_id for game_id in ids if game_id not in registered]
                    details.append(
                        "从当前 app.js 推断的游戏进度：%s/%s；已注册=%s；缺失=%s。"
                        % (
                            len(registered),
                            len(ids),
                            ",".join(registered) or "无",
                            ",".join(missing) or "无",
                        )
                    )
        acceptance_path = self.config.root / "tests" / "test_site_acceptance.py"
        if acceptance_path.is_file():
            try:
                acceptance = acceptance_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                acceptance = ""
            if (
                "registered_game_ids(source, ids)" not in acceptance
                and "registerGame\\s*\\(" in acceptance
            ):
                details.append(
                    "此任务由旧版 Harness 创建，固定验收只识别 registerGame 的字符串 "
                    "literal 首参数；验证前将语义等价的 game.id 调用规范化为对应 manifest ID。"
                )
        return "[Harness Context Checkpoint]\n" + "\n".join(details)

    @staticmethod
    def _bound_tool_message(original: Dict[str, Any]) -> Dict[str, Any]:
        message = dict(original)
        if message.get("role") != "tool":
            return message
        try:
            payload = json.loads(str(message.get("content") or "{}"))
        except (json.JSONDecodeError, TypeError):
            return message
        if not isinstance(payload, dict):
            return message

        changed = False
        for key in ("content", "stdout", "stderr"):
            value = payload.get(key)
            if not isinstance(value, str) or len(value) <= MODEL_TOOL_TEXT_MAX_CHARS:
                continue
            keep = MODEL_TOOL_TEXT_MAX_CHARS // 2
            payload[key] = (
                value[:keep]
                + "\n...[Harness bounded this model-visible tool output; "
                "re-read a smaller range for exact content]...\n"
                + value[-keep:]
            )
            payload["harness_context_truncated"] = True
            changed = True
        if changed:
            message["content"] = json.dumps(payload, ensure_ascii=False)
        return message

    def _context_history(self) -> List[Dict[str, Any]]:
        """Project a bounded recent window while keeping full history and Trace."""
        assistant_indexes = [
            index
            for index, message in enumerate(self.history)
            if message.get("role") == "assistant"
        ]
        if len(assistant_indexes) <= HISTORY_TAIL_ASSISTANT_TURNS:
            selected = list(self.history)
        else:
            cutoff = assistant_indexes[-HISTORY_TAIL_ASSISTANT_TURNS]
            pinned = [
                message
                for message in self.history[:cutoff]
                if message.get("role") == "user"
            ]
            selected = [
                *pinned,
                {"role": "system", "content": self._workspace_checkpoint()},
                *self.history[cutoff:],
            ]
        return [self._bound_tool_message(message) for message in selected]

    def _prefers_exact_edit(self) -> bool:
        """Match OpenCode's model-aware editor choice for MiniMax-family models."""
        return "minimax" in str(getattr(self.model, "name", "")).lower()

    def _action_guard_message(self) -> Optional[str]:
        if self.analysis_calls_since_change >= 10:
            return (
                "[Harness Action Guard]\n"
                "你已连续进行了 10 次成功的读取或辅助分析而没有代码变更。"
                "本轮只提供修改、验证或阻塞上报工具；请立即进行最小可验证修改，"
                "或在确有外部阻塞时调用 report_blocked。"
            )
        if self.analysis_calls_since_change >= 6:
            return (
                "[Harness Action Guard]\n"
                "你已连续进行了 %s 次成功的读取或辅助分析而没有代码变更。"
                "现有信息应足以开始最小修改；辅助脚本暂时关闭，优先 edit_file/write_file。"
                % self.analysis_calls_since_change
            )
        return None

    def _update_action_progress(
        self,
        call: NormalizedToolCall,
        result: ToolResult,
    ) -> None:
        if call.name in WRITE_TOOLS:
            # A mutation attempt re-opens diagnostics if it fails; a successful
            # mutation also proves the current batch made concrete progress.
            self.analysis_calls_since_change = 0
            if result.ok:
                self.run_successful_mutations += 1
            return
        if call.name == "run_validation":
            self.analysis_calls_since_change = 0
            return
        if result.ok and call.name in (READ_TOOLS | HELPER_TOOLS):
            self.analysis_calls_since_change += 1

    def _build_messages(self) -> List[Dict[str, Any]]:
        allowed_tools = self._allowed_tools()
        policy = build_policy_prompt(
            task_id=self.task_id,
            run_id=self.run_id,
            state=self.state,
            task_text=self.task_text,
            user_note=self.user_notes[-1] if self.user_notes else None,
            approved_plan=(
                self.plan
                if self.execution_mode == ExecutionMode.AUTONOMOUS
                or self.state != TaskState.ANALYZING
                else None
            ),
            config=self.config,
            last_validation=self.last_validation,
            changed_files=self.snapshot.changed_files(),
            remaining_turns=max(0, self.max_turns - self.run_turns),
            remaining_active_seconds=self._remaining_seconds(),
            execution_mode=self.execution_mode,
            workspace_isolated=self.workspace_isolated,
            remaining_helper_runs=max(
                0,
                self.max_helper_runs - self.run_helper_runs,
            ),
            allowed_tools=allowed_tools,
        )
        messages = [
            {"role": "system", "content": BASE_SYSTEM_PROMPT},
            {"role": "system", "content": policy},
        ]
        if self._prefers_exact_edit():
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "[Harness Model Adapter: MiniMax]\n"
                        "当前模型使用 OpenCode 风格的精确编辑适配：局部修改使用 edit_file，"
                        "先从最新 read_file 结果复制完全匹配且唯一的 old_string；"
                        "创建文件或分批追加使用 write_file。apply_patch 不对当前模型开放，"
                        "不要生成 unified diff。每次编辑失败后先依据结构化错误校正，"
                        "不要重复提交相同参数。"
                    ),
                }
            )
        guard = self._action_guard_message()
        if guard:
            messages.append({"role": "system", "content": guard})
        messages.extend(self._context_history())
        return messages

    @staticmethod
    def _assistant_message(response: ModelResponse) -> Dict[str, Any]:
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": response.text,
        }
        message.update(response.provider_message_fields)
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                        ),
                    },
                }
                for call in response.tool_calls
            ]
        return message

    def _tool_message(self, call: NormalizedToolCall, result: ToolResult) -> Dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": call.call_id,
            "content": json.dumps(result.to_payload(), ensure_ascii=False),
        }

    def _allowed_tools(self) -> Sequence[str]:
        if self.execution_mode == ExecutionMode.AUTONOMOUS:
            allowed = set(AUTONOMOUS_ALLOWED)
            if self.plan is not None:
                allowed.discard("submit_plan")
            if self._prefers_exact_edit():
                allowed.discard("apply_patch")
            helper_runner = getattr(self.tools, "helper_runner", None)
            if (
                helper_runner is None
                or not helper_runner.available
                or self.run_helper_runs >= self.max_helper_runs
                or self.analysis_calls_since_change >= 6
            ):
                allowed.difference_update({"write_helper_script", "run_helper_script"})
            if self.analysis_calls_since_change >= 10:
                allowed.difference_update(READ_TOOLS)
            return sorted(allowed)
        if self.state == TaskState.ANALYZING:
            return sorted(ANALYZE_ALLOWED)
        allowed = set(EXECUTE_ALLOWED)
        if self._prefers_exact_edit():
            allowed.discard("apply_patch")
        return sorted(allowed)

    def _available_tool_schemas(self) -> List[Dict[str, Any]]:
        allowed = set(self._allowed_tools())
        return [
            schema
            for schema in TOOL_SCHEMAS
            if schema["function"]["name"] in allowed
        ]

    def _tool_signature(self, call: NormalizedToolCall) -> str:
        return call.name + ":" + json.dumps(
            call.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _check_repeat(self, call: NormalizedToolCall) -> bool:
        signature = self._tool_signature(call)
        if signature == self._last_tool_signature:
            self._repeat_tool_count += 1
        else:
            self._last_tool_signature = signature
            self._repeat_tool_count = 1
        return self._repeat_tool_count >= 3

    def _repeated_tool_error(
        self,
        name: str,
        result: ToolResult,
        call: Optional[NormalizedToolCall] = None,
    ) -> bool:
        prefix = name + ":"
        if result.ok:
            self._tool_error_counts = {
                key: value
                for key, value in self._tool_error_counts.items()
                if not key.startswith(prefix)
            }
            return False
        error_code = str(result.error_code or "unknown")
        key = prefix + error_code
        if error_code == "invalid_tool_call":
            # Invalid arguments can fail for unrelated reasons. Only trip the
            # circuit breaker when the concrete validation message repeats.
            key += ":" + str(result.error_message or "")
        if error_code in {"edit_not_found", "edit_ambiguous"} and call is not None:
            # Exact-edit recovery often needs a few distinct candidates. Stop
            # repeated identical failures, but do not conflate different edits.
            key += ":" + hashlib.sha256(
                self._tool_signature(call).encode("utf-8")
            ).hexdigest()[:16]
        self._tool_error_counts[key] = self._tool_error_counts.get(key, 0) + 1
        count = self._tool_error_counts[key]
        result.data.setdefault("same_error_count", count)
        result.data.setdefault("same_error_limit", 3)
        if name == "apply_patch" and result.error_code == "patch_conflict":
            result.data.setdefault(
                "recovery_instruction",
                "不要继续猜测 hunk 行号；重新读取最新片段，追加代码改用 write_file append",
            )
        return count >= 3

    def _record_tool(
        self,
        call: NormalizedToolCall,
        result: ToolResult,
        duration_ms: int,
    ) -> None:
        self.trace.event(
            "tool_result",
            self.state.value,
            {
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
                "result": result.to_payload(),
                "duration_ms": duration_ms,
            },
        )
        outcome = "成功" if result.ok else "失败"
        self._add_summary("%s：%s" % (call.name, outcome), "tool")

    def _fail(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool,
        resume_state: Optional[TaskState] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.resume_state = resume_state or (
            self.state if self.state in ACTIVE_STATES else TaskState.EXECUTING
        )
        if self.resume_state == TaskState.VALIDATING:
            self.resume_state = self._default_work_state()
        self.last_error = {
            "code": code,
            "message": message,
            "recoverable": recoverable,
            "details": details or {},
        }
        self.failure_recoverable = recoverable
        self._transition(TaskState.FAILED, code)
        self._add_summary("任务失败：%s" % message, "error")

    def _handle_plan(self, call: NormalizedToolCall) -> ToolResult:
        autonomous = self.execution_mode == ExecutionMode.AUTONOMOUS
        if (not autonomous and self.state != TaskState.ANALYZING) or (
            autonomous and self.state not in ACTIVE_STATES
        ):
            return ToolResult.failure(
                "tool_not_allowed",
                "当前状态不允许提交计划",
            )
        try:
            plan = Plan.from_arguments(call.arguments)
            for relative in plan.expected_modified_files:
                path = self.config.resolve_relative(relative, allow_missing=True)
                normalized = path.relative_to(self.config.root).as_posix()
                self.config.assert_writable(normalized)
        except (ValueError, AgentHarnessError) as exc:
            if isinstance(exc, AgentHarnessError):
                return ToolResult.failure(
                    exc.code,
                    exc.message,
                    recoverable=exc.recoverable,
                )
            return ToolResult.failure("invalid_tool_call", str(exc))
        self.plan = plan
        if autonomous:
            if self.state == TaskState.ANALYZING:
                self._transition(TaskState.EXECUTING, "autonomous_plan_submitted")
            self.trace.event(
                "autonomous_plan",
                self.state.value,
                {"plan": plan.to_dict(), "approval_required": False},
            )
            self._add_summary("模型已记录计划并自动继续", "plan")
            return ToolResult.success(
                plan=plan.to_dict(),
                approval_required=False,
            )
        self._transition(TaskState.AWAITING_APPROVAL, "plan_submitted")
        self._add_summary("分析完成，等待批准", "plan")
        return ToolResult.success(plan=plan.to_dict())

    def _handle_blocked(self, call: NormalizedToolCall) -> ToolResult:
        reason = call.arguments.get("reason")
        attempted = call.arguments.get("attempted")
        next_step = call.arguments.get("suggested_next_step")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(attempted, list)
            or not all(isinstance(item, str) for item in attempted)
            or not isinstance(next_step, str)
            or not next_step.strip()
        ):
            return ToolResult.failure(
                "invalid_tool_call",
                "report_blocked 参数不完整",
            )
        result = ToolResult.success(
            reason=reason.strip(),
            attempted=attempted,
            suggested_next_step=next_step.strip(),
        )
        self._fail(
            "model_blocked",
            reason.strip(),
            recoverable=True,
            details=result.data,
        )
        return result

    def _execute_tool(self, call: NormalizedToolCall) -> Tuple[ToolResult, bool]:
        """Return (result, stop_remaining_calls)."""
        self.run_tool_calls += 1
        self.total_tool_calls += 1
        self.trace.event(
            "tool_call",
            self.state.value,
            {
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
            },
        )

        if self._check_repeat(call):
            result = ToolResult.failure(
                "repeated_tool_call",
                "相同工具和参数连续出现 3 次，已停止当前执行",
                recoverable=True,
            )
            self._fail(
                "repeated_tool_call",
                result.error_message or "重复工具调用",
                recoverable=True,
            )
            return result, True

        if call.name not in self._allowed_tools():
            return (
                ToolResult.failure(
                    "tool_not_allowed",
                    "当前阶段不允许调用 %s" % call.name,
                    recoverable=True,
                    allowed_tools=list(self._allowed_tools()),
                ),
                False,
            )

        if call.name == "run_helper_script":
            if self.run_helper_runs >= self.max_helper_runs:
                result = ToolResult.failure(
                    "helper_run_budget_exhausted",
                    "本次执行已达到 %s 次辅助脚本上限" % self.max_helper_runs,
                    recoverable=True,
                )
                self._fail(
                    "helper_run_budget_exhausted",
                    result.error_message or "辅助脚本预算已用完",
                    recoverable=True,
                    details={"max_helper_runs": self.max_helper_runs},
                )
                return result, True
            self.run_helper_runs += 1
            self.total_helper_runs += 1

        if call.name == "submit_plan":
            result = self._handle_plan(call)
            return (
                result,
                result.ok and self.execution_mode == ExecutionMode.SUPERVISED,
            )
        if call.name == "report_blocked":
            result = self._handle_blocked(call)
            return result, True

        if call.name == "run_validation":
            previous_state = self.state
            self._transition(TaskState.VALIDATING, "validation_started")
            self._add_summary("正在运行项目验证", "validation")
            result, validation = self.tools.execute(
                call.name,
                call.arguments,
                stop_event=self.stop_event,
            )
            if validation is None:
                self._fail(
                    result.error_code or "validation_runner_error",
                    result.error_message or "验证工具没有返回结果",
                    recoverable=result.recoverable,
                    resume_state=previous_state,
                )
                return result, True
            self.last_validation = validation
            self.trace.event(
                "validation_result",
                self.state.value,
                validation.to_dict(),
            )
            if validation.cancelled or self.stop_event.is_set():
                self.resume_state = self._default_work_state()
                self._transition(TaskState.CANCELLED, "user_stop")
                self._add_summary("用户已停止验证", "warning")
                return result, True
            if validation.runner_error:
                self._fail(
                    "validation_runner_error",
                    validation.runner_error,
                    recoverable=True,
                    resume_state=previous_state,
                )
                return result, True
            if validation.passed:
                if not self.snapshot.changed_files():
                    no_changes = ToolResult.failure(
                        "validation_no_changes",
                        "验证命令通过，但任务尚未产生任何工程代码变更",
                        recoverable=True,
                        validation=validation.to_dict(),
                    )
                    self._transition(
                        self._default_work_state(),
                        "validation_passed_without_changes",
                    )
                    self._add_summary("验证通过但没有代码变更，继续执行", "warning")
                    return no_changes, False
                try:
                    self.snapshot.assert_touched_current()
                except AgentHarnessError as exc:
                    conflict = ToolResult.failure(
                        exc.code,
                        exc.message,
                        recoverable=exc.recoverable,
                        **exc.details,
                    )
                    self._fail(
                        exc.code,
                        exc.message,
                        recoverable=exc.recoverable,
                        resume_state=previous_state,
                        details=exc.details,
                    )
                    return conflict, True
                self.dirty_since_validation = False
                self._transition(TaskState.SUCCESS, "validation_passed")
                self._add_summary("项目验证通过", "success")
                return result, True
            self.dirty_since_validation = False
            self._transition(TaskState.REPAIRING, "validation_failed")
            self._add_summary("验证未通过，进入自主修复", "warning")
            return result, False

        result, _ = self.tools.execute(
            call.name,
            call.arguments,
            stop_event=self.stop_event,
        )
        if self._repeated_tool_error(call.name, result, call):
            self._fail(
                "repeated_tool_error",
                "%s 在没有成功变更期间累计 3 次返回同类错误 %s，已暂停避免无效重试"
                % (call.name, result.error_code or "unknown"),
                recoverable=True,
                details={
                    "tool": call.name,
                    "tool_error_code": result.error_code,
                },
            )
            return result, True
        if call.name in WRITE_TOOLS and result.ok:
            self._tool_error_counts = {}
            self.dirty_since_validation = True
            changed = result.data.get("changed_files", [])
            self.trace.event(
                "diff_updated",
                self.state.value,
                {
                    "changed_files": changed,
                    "diff_summary": result.data.get("diff_summary"),
                },
            )
        if not result.ok and not result.recoverable:
            self._fail(
                result.error_code or "tool_internal_error",
                result.error_message or "工具发生不可恢复错误",
                recoverable=False,
            )
            return result, True
        return result, False

    def _budget_exhausted(self) -> bool:
        return (
            self.run_turns >= self.max_turns
            or self._active_seconds() >= self.max_active_seconds
        )

    def _run_budget_reason(self) -> Optional[str]:
        if self.run_turns >= self.max_turns:
            return "turns"
        if self._active_seconds() >= self.max_active_seconds:
            return "active_seconds"
        return None

    def _can_auto_continue_batch(self) -> bool:
        return (
            self.execution_mode == ExecutionMode.AUTONOMOUS
            and self._total_budget_reason() is None
            and self.run_successful_mutations > 0
        )

    def _auto_continue_run(self) -> None:
        """Roll a productive autonomous batch without asking the user."""
        old_run_id = self.run_id
        self._finish_run("batch_rollover")
        self.run_id = str(uuid.uuid4())
        self.trace = self.trace.with_run(
            self.task_dir / "runs" / self.run_id / "trace.jsonl",
            self.run_id,
        )
        self.run_turns = 0
        self.run_tool_calls = 0
        self.run_helper_runs = 0
        self.run_active_seconds = 0.0
        self._active_segment_started = None
        self._run_finished = False
        self.run_usage = {}
        self.run_successful_mutations = 0
        self._last_tool_signature = None
        self._repeat_tool_count = 0
        self._tool_error_counts = {}
        self._no_tool_streak = 0
        self.budget_scope = None
        self.budget_reason = None
        self.trace.event(
            "run_started",
            self.state.value,
            {
                "continued_from_run_id": old_run_id,
                "continuation": "automatic_batch_rollover",
                "max_turns": self.max_turns,
                "max_active_seconds": self.max_active_seconds,
                "max_total_turns": self.max_total_turns,
                "max_total_active_seconds": self.max_total_active_seconds,
                "max_total_tokens": self.max_total_tokens,
                "max_helper_runs": self.max_helper_runs,
            },
        )
        self.history.append(
            {
                "role": "system",
                "content": (
                    "[Harness Automatic Batch Continuation]\n"
                    "上一执行批次已产生有效代码变更，并在任务总预算内自动续批次。"
                    "当前工作区与任务目标不变；继续修改并运行验证，不要从零重做。"
                ),
            }
        )
        self._add_summary("单批次预算已用完，Harness 已自动续批次", "info")
        self._begin_active_segment()

    def _enter_budget_exhausted(
        self,
        *,
        scope: str = "batch",
        reason: Optional[str] = None,
    ) -> None:
        self.resume_state = (
            self.state if self.state in ACTIVE_STATES else TaskState.EXECUTING
        )
        self.budget_scope = scope
        self.budget_reason = reason or (
            self._total_budget_reason() if scope == "task" else self._run_budget_reason()
        )
        self._transition(
            TaskState.BUDGET_EXHAUSTED,
            "budget_exhausted",
            budget_scope=self.budget_scope,
            budget_reason=self.budget_reason,
            run_turns=self.run_turns,
            total_turns=self.total_turns,
            active_seconds=round(self._active_seconds(), 3),
            total_active_seconds=round(self._total_active_seconds_value(), 3),
            total_tokens=self._total_token_count(),
        )
        if scope == "task":
            self._add_summary("达到任务总预算，已保留现场", "warning")
        elif self.run_successful_mutations == 0:
            self._add_summary(
                "单批次预算已用完且未产生有效代码变更，已暂停避免空转",
                "warning",
            )
        else:
            self._add_summary("达到当前执行批次预算，已保留现场", "warning")

    def run_until_pause(self) -> TaskState:
        """Run synchronously until approval, terminal state, or user stop."""
        with self._lock:
            if self._worker_active:
                return self.state
            if self.state not in ACTIVE_STATES:
                return self.state
            self._worker_active = True
            self._begin_active_segment()

        try:
            while True:
                with self._lock:
                    if self.state not in ACTIVE_STATES:
                        break
                    if self.stop_event.is_set():
                        self.resume_state = (
                            TaskState.REPAIRING
                            if self.state == TaskState.VALIDATING and self.plan
                            else self.state
                        )
                        self._transition(TaskState.CANCELLED, "user_stop")
                        self._add_summary("用户已停止当前任务", "warning")
                        break
                    total_budget_reason = self._total_budget_reason()
                    if total_budget_reason is not None:
                        self._enter_budget_exhausted(
                            scope="task",
                            reason=total_budget_reason,
                        )
                        break
                    if self._budget_exhausted():
                        if self._can_auto_continue_batch():
                            self._auto_continue_run()
                            continue
                        self._enter_budget_exhausted(
                            scope="batch",
                            reason=self._run_budget_reason(),
                        )
                        break
                    messages = self._build_messages()
                    tool_schemas = self._available_tool_schemas()
                    request_state = self.state
                    self.trace.event(
                        "model_request",
                        self.state.value,
                        {
                            "model": self.model.name,
                            "messages": trace_safe_messages(messages),
                            "tools": [
                                schema["function"]["name"]
                                for schema in tool_schemas
                            ],
                            "turn": self.run_turns + 1,
                        },
                    )

                try:
                    response = self.model.complete(
                        messages,
                        tool_schemas,
                        timeout_seconds=self.model_timeout_seconds,
                    )
                except ModelAPIError as exc:
                    with self._lock:
                        self._fail(
                            exc.code,
                            exc.message,
                            recoverable=exc.recoverable,
                            resume_state=request_state,
                            details=exc.details,
                        )
                    break
                except Exception as exc:
                    with self._lock:
                        self._fail(
                            "model_service_error",
                            "模型调用异常：%s" % exc,
                            recoverable=True,
                            resume_state=request_state,
                        )
                    break

                with self._lock:
                    self.run_turns += 1
                    self.total_turns += 1
                    self._update_usage(response.usage)
                    self.trace.event(
                        "model_response",
                        self.state.value,
                        {
                            "text": trace_visible_text(response.text),
                            "tool_calls": [
                                {
                                    "call_id": call.call_id,
                                    "name": call.name,
                                    "arguments": call.arguments,
                                }
                                for call in response.tool_calls
                            ],
                            "stop_reason": response.stop_reason,
                            "usage": response.usage,
                            "response_id": response.raw_response_id,
                        },
                    )
                    self.history.append(self._assistant_message(response))

                    if self.stop_event.is_set():
                        self.resume_state = request_state
                        if self.resume_state == TaskState.VALIDATING:
                            self.resume_state = self._default_work_state()
                        self._transition(TaskState.CANCELLED, "user_stop_after_model_response")
                        self._add_summary("用户已停止；模型返回的工具未执行", "warning")
                        break

                    if not response.tool_calls:
                        self._no_tool_streak += 1
                        if self._no_tool_streak >= 3:
                            self._fail(
                                "model_no_progress",
                                "模型连续 3 轮没有调用完成任务所需的工具",
                                recoverable=True,
                                resume_state=request_state,
                            )
                            break
                        self.history.append(
                            {
                                "role": "system",
                                "content": (
                                    "当前任务尚未满足成功条件。请调用允许的工具继续；"
                                    + (
                                        "自主决定读取、修改、辅助脚本和验证步骤，修改后必须 run_validation。"
                                        if self.execution_mode
                                        == ExecutionMode.AUTONOMOUS
                                        else "分析阶段使用 submit_plan，执行阶段修改后必须 run_validation。"
                                    )
                                ),
                            }
                        )
                        continue

                    self._no_tool_streak = 0
                    prior_failed = False
                    for call in response.tool_calls:
                        started = time.monotonic()
                        if prior_failed or self.state not in ACTIVE_STATES:
                            result = ToolResult.failure(
                                "not_executed_due_to_prior_error",
                                "同一响应中的前序工具失败或状态已暂停",
                                recoverable=True,
                            )
                            stop_remaining = True
                        else:
                            result, stop_remaining = self._execute_tool(call)
                            self._update_action_progress(call, result)
                        duration_ms = int((time.monotonic() - started) * 1000)
                        self._record_tool(call, result, duration_ms)
                        self.history.append(self._tool_message(call, result))
                        prior_failed = stop_remaining or not result.ok
                        if self.state not in ACTIVE_STATES:
                            prior_failed = True

                    if self.state in ACTIVE_STATES:
                        self.history.append(
                            {
                                "role": "system",
                                "content": tool_result_followup(
                                    self.state,
                                    self.execution_mode,
                                    self._allowed_tools(),
                                ),
                            }
                        )
        finally:
            with self._lock:
                self._end_active_segment()
                self._worker_active = False
                if self.state in TERMINAL_RUN_STATES:
                    self._finish_run()
        return self.state

    def approve_plan(self) -> None:
        with self._lock:
            if self.state != TaskState.AWAITING_APPROVAL or self.plan is None:
                raise ValueError("当前没有等待批准的计划")
            self.trace.event("approval", self.state.value, {"decision": "approved"})
            self._transition(TaskState.EXECUTING, "user_approved")
            self._add_summary("用户已批准执行计划", "approval")

    def reject_plan(self) -> None:
        with self._lock:
            if self.state != TaskState.AWAITING_APPROVAL:
                raise ValueError("当前没有等待处理的计划")
            if self.snapshot.diff():
                self._fail(
                    "approval_boundary_broken",
                    "批准前工作区已经发生 Agent 修改",
                    recoverable=False,
                )
            else:
                self.trace.event("approval", self.state.value, {"decision": "rejected"})
                self._transition(TaskState.REJECTED, "user_rejected")
                self._add_summary("用户拒绝了执行计划", "warning")
                self._finish_run()

    def request_stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            self.trace.event("user_stop", self.state.value, {})
            if not self._worker_active and self.state == TaskState.AWAITING_APPROVAL:
                self.resume_state = TaskState.AWAITING_APPROVAL
                self._transition(TaskState.CANCELLED, "user_stop")
                self._add_summary("用户已停止当前任务", "warning")
                self._finish_run()

    def continue_task(self, user_note: Optional[str] = None) -> bool:
        """Start a new Run. Return whether a background worker should be started."""
        with self._lock:
            if self.state not in {
                TaskState.CANCELLED,
                TaskState.BUDGET_EXHAUSTED,
                TaskState.FAILED,
            }:
                raise ValueError("当前状态不能继续")
            if self.state == TaskState.FAILED and not self.failure_recoverable:
                raise ValueError("当前错误不可恢复")
            if self._total_budget_reason() is not None:
                raise ValueError("任务总预算已用完，请新建任务或由开发者调整总预算")
            old_run_id = self.run_id
            self.run_id = str(uuid.uuid4())
            self.trace = self.trace.with_run(
                self.task_dir / "runs" / self.run_id / "trace.jsonl",
                self.run_id,
            )
            self.run_turns = 0
            self.run_tool_calls = 0
            self.run_helper_runs = 0
            self.run_active_seconds = 0.0
            self._active_segment_started = None
            self._run_finished = False
            self.stop_event.clear()
            self._last_tool_signature = None
            self._repeat_tool_count = 0
            self._tool_error_counts = {}
            self._no_tool_streak = 0
            self.analysis_calls_since_change = 0
            self.run_successful_mutations = 0
            self.budget_scope = None
            self.budget_reason = None
            self.run_usage = {}
            self.last_error = None
            self.failure_recoverable = False
            note = (user_note or "").strip()
            if note:
                self.user_notes.append(note)
                self.history.append({"role": "user", "content": "补充说明：" + note})
            target = self.resume_state
            if target == TaskState.VALIDATING:
                target = self._default_work_state()
            self.state = target
            self.trace.event(
                "run_started",
                self.state.value,
                {
                    "continued_from_run_id": old_run_id,
                    "max_turns": self.max_turns,
                    "max_active_seconds": self.max_active_seconds,
                    "max_total_turns": self.max_total_turns,
                    "max_total_active_seconds": self.max_total_active_seconds,
                    "max_total_tokens": self.max_total_tokens,
                    "max_helper_runs": self.max_helper_runs,
                    "user_note": note or None,
                },
            )
            self.trace.event(
                "user_continue",
                self.state.value,
                {"previous_run_id": old_run_id, "user_note": note or None},
            )
            self._add_summary("继续当前任务，已创建新的执行批次", "info")
            return self.state in ACTIVE_STATES

    def retry_task(self, user_note: Optional[str] = None) -> bool:
        return self.continue_task(user_note)

    def keep_and_finish(self) -> None:
        with self._lock:
            if self._worker_active:
                raise ValueError("执行中不能直接保留结束，请先停止")
            self._finalized = True
            self.trace.event(
                "task_finished",
                self.state.value,
                {
                    "final_state": self.state.value,
                    "kept_unverified": self.state != TaskState.SUCCESS,
                    "changed_files": self.snapshot.changed_files(),
                    "diff": self.snapshot.diff(),
                },
            )
            self._add_summary("已保留当前修改并结束任务", "info")

    def revert_task(self) -> Dict[str, Any]:
        with self._lock:
            if self._worker_active:
                raise ValueError("执行中不能撤销，请先停止")
            result = self.snapshot.revert()
            self.trace.event("revert_result", self.state.value, result.to_dict())
            if result.ok:
                self._transition(TaskState.REVERTED, "user_reverted")
                self._add_summary("已撤销本次 Agent 修改", "success")
                self._finalized = True
            else:
                self._fail(
                    "revert_partial_failure",
                    "部分文件无法恢复",
                    recoverable=True,
                    details=result.to_dict(),
                )
            self._finish_run()
            if result.ok:
                self.trace.event(
                    "task_finished",
                    self.state.value,
                    {
                        "final_state": self.state.value,
                        "revert": result.to_dict(),
                        "changed_files": self.snapshot.changed_files(),
                        "diff": self.snapshot.diff(),
                    },
                )
            return result.to_dict()

    @property
    def worker_active(self) -> bool:
        with self._lock:
            return self._worker_active

    def get_view(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "task_id": self.task_id,
                "run_id": self.run_id,
                "state": self.state.value,
                "worker_active": self._worker_active,
                "finalized": self._finalized,
                "workspace": self.config.public_summary(),
                "task": self.task_text,
                "model": self.model.name,
                "execution_mode": self.execution_mode.value,
                "workspace_isolated": self.workspace_isolated,
                "source_workspace": str(self.source_workspace),
                "plan": self.plan.to_dict() if self.plan else None,
                "events": list(self.event_summaries),
                "last_validation": (
                    self.last_validation.to_dict() if self.last_validation else None
                ),
                "changed_files": self.snapshot.changed_files(),
                "diff": self.snapshot.diff(),
                "run_turns": self.run_turns,
                "total_turns": self.total_turns,
                "run_tool_calls": self.run_tool_calls,
                "total_tool_calls": self.total_tool_calls,
                "run_helper_runs": self.run_helper_runs,
                "total_helper_runs": self.total_helper_runs,
                "max_helper_runs": self.max_helper_runs,
                "run_active_seconds": round(self._active_seconds(), 2),
                "total_active_seconds": round(
                    self.total_active_seconds
                    + (
                        time.monotonic() - self._active_segment_started
                        if self._active_segment_started is not None
                        else 0
                    ),
                    2,
                ),
                "max_turns": self.max_turns,
                "max_active_seconds": self.max_active_seconds,
                "max_total_turns": self.max_total_turns,
                "max_total_active_seconds": self.max_total_active_seconds,
                "max_total_tokens": self.max_total_tokens,
                "total_tokens": self._total_token_count(),
                "budget_scope": self.budget_scope,
                "budget_reason": self.budget_reason,
                "analysis_calls_since_change": self.analysis_calls_since_change,
                "run_successful_mutations": self.run_successful_mutations,
                "run_usage": dict(self.run_usage),
                "usage": dict(self.usage),
                "last_error": dict(self.last_error) if self.last_error else None,
                "failure_recoverable": self.failure_recoverable,
                "trace_path": str(self.trace.path),
                "task_dir": str(self.task_dir),
            }
