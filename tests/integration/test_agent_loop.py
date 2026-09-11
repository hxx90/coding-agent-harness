from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path

from agent_harness.domain import (
    ExecutionMode,
    ModelResponse,
    NormalizedToolCall,
    TaskState,
    ToolResult,
)
from agent_harness.model_adapter import (
    DemoModelAdapter,
    ModelAdapter,
    ScriptedModelAdapter,
)
from agent_harness.orchestrator import TaskOrchestrator
from agent_harness.project_bootstrap import create_website_workspace


E01_TASK = (
    "用户注册时，空用户名没有返回错误。请定位并修复。"
    "None、空字符串和只包含空白字符都应视为无效。"
    "运行现有测试验证，不要修改无关功能，也不要修改测试。"
)

E02_TASK = (
    "请在订单模块增加 calculate_discount 函数，"
    "严格按照 README 中的折扣规则实现，并运行现有测试。"
    "不要修改 README 和测试，不要增加任务之外的功能。"
)


def call(name, arguments):
    return ModelResponse(
        tool_calls=[
            NormalizedToolCall(
                call_id="test-%s-%s" % (name, time.time_ns()),
                name=name,
                arguments=arguments,
            )
        ],
        stop_reason="tool_calls",
    )


def plan_call():
    return call(
        "submit_plan",
        {
            "task_summary": "修复用户名",
            "relevant_files": ["src/user.py"],
            "planned_changes": ["补全空值校验"],
            "expected_modified_files": ["src/user.py"],
            "validation_plan": "运行 pytest",
            "risks_or_questions": [],
        },
    )


def trace_events(task_dir: Path):
    events = []
    pattern = "*" + "/trace.jsonl"
    for path in sorted((task_dir / "runs").glob(pattern)):
        events.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    # Run directories use random UUIDs, so path order is unrelated to execution
    # order once an autonomous task rolls into a new batch.
    return sorted(events, key=lambda event: event["timestamp"])


def test_e01_demo_repairs_after_failed_validation(workspace_factory, runtime_dir):
    workspace = workspace_factory("e01_bugfix")
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=DemoModelAdapter(),
        data_dir=runtime_dir,
    )

    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    assert not agent.snapshot.diff()
    agent.approve_plan()
    assert agent.run_until_pause() == TaskState.SUCCESS

    view = agent.get_view()
    assert view["last_validation"]["exit_code"] == 0
    assert view["changed_files"] == ["src/user.py"]
    assert view["total_turns"] == 7
    assert (workspace / "tests" / "test_user.py").read_text(encoding="utf-8").startswith(
        "import pytest"
    )

    events = trace_events(agent.task_dir)
    states = [
        event["data"]["to"]
        for event in events
        if event["event_type"] == "state_changed"
    ]
    assert "repairing" in states
    assert states[-1] == "success"
    validations = [
        event for event in events if event["event_type"] == "validation_result"
    ]
    assert len(validations) == 2
    assert validations[0]["data"]["exit_code"] != 0
    assert validations[1]["data"]["exit_code"] == 0


def test_e02_demo_completes_feature(workspace_factory, runtime_dir):
    workspace = workspace_factory("e02_feature")
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E02_TASK,
        model=DemoModelAdapter(),
        data_dir=runtime_dir,
    )
    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    agent.approve_plan()
    assert agent.run_until_pause() == TaskState.SUCCESS
    view = agent.get_view()
    assert view["changed_files"] == ["src/order.py"]
    assert "calculate_discount" in (workspace / "src" / "order.py").read_text(
        encoding="utf-8"
    )
    assert view["last_validation"]["exit_code"] == 0


def test_reject_plan_keeps_workspace_unchanged(workspace_factory, runtime_dir):
    workspace = workspace_factory("e01_bugfix")
    before = (workspace / "src" / "user.py").read_bytes()
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=ScriptedModelAdapter([plan_call()]),
        data_dir=runtime_dir,
    )
    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    agent.reject_plan()
    assert agent.state == TaskState.REJECTED
    assert (workspace / "src" / "user.py").read_bytes() == before


def test_budget_exhausted_then_continue_same_task(workspace_factory, runtime_dir):
    workspace = workspace_factory("e01_bugfix")
    responses = [
        plan_call(),
        call("apply_patch", {"patch": DemoModelAdapter.USERNAME_FIRST_PATCH}),
        call("apply_patch", {"patch": DemoModelAdapter.USERNAME_REPAIR_PATCH}),
        call("run_validation", {}),
    ]
    for response in responses:
        response.usage = {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        }
    adapter = ScriptedModelAdapter(responses)
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
        max_turns=2,
    )
    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    task_id = agent.task_id
    first_run = agent.run_id
    agent.approve_plan()
    assert agent.run_until_pause() == TaskState.BUDGET_EXHAUSTED
    assert agent.get_view()["run_usage"]["prompt_tokens"] == 20
    assert 'username == ""' in (workspace / "src" / "user.py").read_text(
        encoding="utf-8"
    )

    assert agent.continue_task("继续修复空白字符串") is True
    assert agent.get_view()["run_usage"] == {}
    assert agent.task_id == task_id
    assert agent.run_id != first_run
    assert agent.run_until_pause() == TaskState.SUCCESS
    assert agent.get_view()["run_usage"]["prompt_tokens"] == 20
    assert agent.get_view()["usage"]["prompt_tokens"] == 40
    pattern = "*" + "/trace.jsonl"
    assert len(list((agent.task_dir / "runs").glob(pattern))) == 2
    finished = [
        event
        for event in trace_events(agent.task_dir)
        if event["event_type"] == "run_finished"
    ]
    assert [event["data"]["usage"]["prompt_tokens"] for event in finished] == [20, 20]
    successful_run = next(event for event in finished if event["state"] == "success")
    assert successful_run["data"]["total_usage"]["prompt_tokens"] == 40
    assert "not username.strip()" in agent.get_view()["diff"]


def test_minimax_uses_exact_edit_instead_of_unified_patch(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e01_bugfix")
    adapter = ScriptedModelAdapter(
        [
            call(
                "edit_file",
                {
                    "path": "src/user.py",
                    "old_string": "if username is None:",
                    "new_string": "if username is None or not username.strip():",
                },
            ),
            call("run_validation", {}),
        ]
    )
    adapter.name = "openai-compatible/MiniMax-M3"
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
    )

    assert agent.run_until_pause() == TaskState.SUCCESS
    for request in adapter.requests:
        names = {schema["function"]["name"] for schema in request["tools"]}
        assert "edit_file" in names
        assert "apply_patch" not in names
    system_text = "\n".join(
        message.get("content", "")
        for message in adapter.requests[0]["messages"]
        if message.get("role") == "system"
    )
    assert "Harness Model Adapter: MiniMax" in system_text


def test_productive_autonomous_batch_rolls_over_without_user_action(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e01_bugfix")
    adapter = ScriptedModelAdapter(
        [
            call(
                "edit_file",
                {
                    "path": "src/user.py",
                    "old_string": "if username is None:",
                    "new_string": 'if username is None or username == "":',
                },
            ),
            call(
                "edit_file",
                {
                    "path": "src/user.py",
                    "old_string": 'if username is None or username == "":',
                    "new_string": "if username is None or not username.strip():",
                },
            ),
            call("run_validation", {}),
        ]
    )
    adapter.name = "openai-compatible/MiniMax-M3"
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
        max_turns=2,
        max_total_turns=6,
    )

    assert agent.run_until_pause() == TaskState.SUCCESS
    assert agent.total_turns == 3
    finished = [
        event
        for event in trace_events(agent.task_dir)
        if event["event_type"] == "run_finished"
    ]
    assert len(finished) == 2
    assert finished[0]["data"]["finish_reason"] == "batch_rollover"
    assert finished[1]["state"] == "success"
    assert any(
        "Harness 已自动续批次" in event["text"]
        for event in agent.get_view()["events"]
    )


def test_action_guard_hides_analysis_tools_after_no_change(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e01_bugfix")
    adapter = ScriptedModelAdapter([])
    adapter.name = "openai-compatible/MiniMax-M3"
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
    )

    agent.analysis_calls_since_change = 6
    names = set(agent._allowed_tools())
    assert "read_file" in names
    assert "run_helper_script" not in names

    agent.analysis_calls_since_change = 10
    names = set(agent._allowed_tools())
    assert names.isdisjoint({"list_files", "search_code", "read_file"})
    assert "edit_file" in names
    assert "write_file" in names
    assert "run_validation" in names
    assert "apply_patch" not in names


def test_automatic_rollover_stops_at_task_total_budget(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e01_bugfix")
    adapter = ScriptedModelAdapter(
        [
            call(
                "edit_file",
                {
                    "path": "src/user.py",
                    "old_string": "if username is None:",
                    "new_string": 'if username is None or username == "":',
                },
            ),
            call(
                "edit_file",
                {
                    "path": "src/user.py",
                    "old_string": 'if username is None or username == "":',
                    "new_string": "if username is None or not username.strip():",
                },
            ),
        ]
    )
    adapter.name = "openai-compatible/MiniMax-M3"
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
        max_turns=1,
        max_total_turns=2,
    )

    assert agent.run_until_pause() == TaskState.BUDGET_EXHAUSTED
    view = agent.get_view()
    assert view["budget_scope"] == "task"
    assert view["budget_reason"] == "turns"
    assert view["total_turns"] == 2
    assert len(adapter.requests) == 2


def test_only_currently_allowed_tool_schemas_are_sent(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e01_bugfix")
    adapter = ScriptedModelAdapter(
        [
            call("list_files", {"directory": "", "max_results": 10}),
            plan_call(),
        ]
    )
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
    )
    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    assert len(adapter.requests) == 2
    expected = {"list_files", "search_code", "read_file", "submit_plan"}
    assert all(
        {schema["function"]["name"] for schema in request["tools"]} == expected
        for request in adapter.requests
    )


def test_provider_reasoning_metadata_is_returned_on_next_model_turn(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e01_bugfix")
    first = call("list_files", {"directory": "", "max_results": 10})
    first.text = "<think>private chain</think>visible summary"
    first.provider_message_fields = {
        "reasoning_details": [{"type": "text", "text": "opaque"}]
    }
    adapter = ScriptedModelAdapter([first, plan_call()])
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
    )

    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    second_messages = adapter.requests[1]["messages"]
    assistant = next(
        message
        for message in second_messages
        if message.get("role") == "assistant"
    )
    assert assistant["reasoning_details"] == [
        {"type": "text", "text": "opaque"}
    ]
    assert assistant["content"] == "<think>private chain</think>visible summary"
    trace_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (agent.task_dir / "runs").glob("*/trace.jsonl")
    )
    assert "private chain" not in trace_text
    assert "opaque" not in trace_text
    assert "visible summary" in trace_text


def test_model_text_only_cannot_claim_success(workspace_factory, runtime_dir):
    workspace = workspace_factory("e01_bugfix")
    adapter = ScriptedModelAdapter(
        [
            ModelResponse(text="已经完成"),
            ModelResponse(text="已经完成"),
            ModelResponse(text="已经完成"),
        ]
    )
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
    )
    assert agent.run_until_pause() == TaskState.FAILED
    assert agent.get_view()["last_error"]["code"] == "model_no_progress"


def test_three_same_tool_errors_trip_circuit_breaker(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e01_bugfix")
    adapter = ScriptedModelAdapter(
        [
            plan_call(),
            call("apply_patch", {"patch": "invalid patch one"}),
            call("apply_patch", {"patch": "invalid patch two"}),
            call("apply_patch", {"patch": "invalid patch three"}),
            call("apply_patch", {"patch": "invalid patch four"}),
        ]
    )
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
    )
    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    agent.approve_plan()

    assert agent.run_until_pause() == TaskState.FAILED
    assert agent.get_view()["last_error"]["code"] == "repeated_tool_error"
    assert agent.total_tool_calls == 4  # plan + three rejected patches


def test_user_can_stop_running_validation(workspace_factory, runtime_dir):
    workspace = workspace_factory(
        "e01_bugfix",
        validation_command=[
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
        ],
    )
    adapter = ScriptedModelAdapter(
        [
            plan_call(),
            call("apply_patch", {"patch": DemoModelAdapter.USERNAME_FIRST_PATCH}),
            call("run_validation", {}),
        ]
    )
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
    )
    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    agent.approve_plan()
    worker = threading.Thread(target=agent.run_until_pause, daemon=True)
    worker.start()

    deadline = time.monotonic() + 3
    while agent.state != TaskState.VALIDATING and time.monotonic() < deadline:
        time.sleep(0.02)
    assert agent.state == TaskState.VALIDATING
    agent.request_stop()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert agent.state == TaskState.CANCELLED
    assert agent.get_view()["changed_files"] == ["src/user.py"]
    reverted = agent.revert_task()
    assert reverted["ok"]
    assert agent.state == TaskState.REVERTED


def test_stop_during_model_call_discards_returned_write(
    workspace_factory, runtime_dir
):
    class BlockingWriteAdapter(ModelAdapter):
        name = "blocking-write"

        def __init__(self):
            self.calls = 0
            self.request_started = threading.Event()
            self.release_response = threading.Event()

        def complete(self, messages, tools, timeout_seconds=60):
            self.calls += 1
            if self.calls == 1:
                return plan_call()
            self.request_started.set()
            assert self.release_response.wait(timeout=3)
            return call(
                "apply_patch",
                {"patch": DemoModelAdapter.USERNAME_FIRST_PATCH},
            )

    workspace = workspace_factory("e01_bugfix")
    before = (workspace / "src" / "user.py").read_bytes()
    adapter = BlockingWriteAdapter()
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
    )
    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    agent.approve_plan()

    worker = threading.Thread(target=agent.run_until_pause, daemon=True)
    worker.start()
    assert adapter.request_started.wait(timeout=3)
    agent.request_stop()
    adapter.release_response.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert agent.state == TaskState.CANCELLED
    assert (workspace / "src" / "user.py").read_bytes() == before
    assert agent.get_view()["changed_files"] == []
    assert agent.total_tool_calls == 1  # submit_plan only


def test_validation_cannot_hide_external_file_change(
    workspace_factory, runtime_dir
):
    validation_script = (
        "from pathlib import Path; "
        "p=Path('src/user.py'); "
        "p.write_text(p.read_text(encoding='utf-8') + '# external change\\n', "
        "encoding='utf-8')"
    )
    workspace = workspace_factory(
        "e01_bugfix",
        validation_command=[sys.executable, "-c", validation_script],
    )
    adapter = ScriptedModelAdapter(
        [
            plan_call(),
            call("apply_patch", {"patch": DemoModelAdapter.USERNAME_FIRST_PATCH}),
            call("run_validation", {}),
        ]
    )
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
    )
    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    agent.approve_plan()

    assert agent.run_until_pause() == TaskState.FAILED
    assert agent.last_validation is not None
    assert agent.last_validation.passed
    assert agent.get_view()["last_error"]["code"] == "concurrent_modification"


def test_revert_records_reverted_task_as_final_trace_state(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e02_feature")
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E02_TASK,
        model=DemoModelAdapter(),
        data_dir=runtime_dir,
    )
    assert agent.run_until_pause() == TaskState.AWAITING_APPROVAL
    agent.approve_plan()
    assert agent.run_until_pause() == TaskState.SUCCESS

    assert agent.revert_task()["ok"]
    finished = [
        event
        for event in trace_events(agent.task_dir)
        if event["event_type"] == "task_finished"
    ]
    assert finished[-1]["data"]["final_state"] == "reverted"


def test_autonomous_mode_skips_approval_and_modifies_only_isolated_copy(
    workspace_factory, runtime_dir
):
    source_workspace = workspace_factory("e01_bugfix")
    source_before = (source_workspace / "src" / "user.py").read_bytes()
    agent = TaskOrchestrator(
        workspace=str(source_workspace),
        task_text=E01_TASK,
        model=DemoModelAdapter(),
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
    )

    assert agent.run_until_pause() == TaskState.SUCCESS
    assert agent.config.root != source_workspace
    assert (source_workspace / "src" / "user.py").read_bytes() == source_before
    assert "not username.strip()" in (
        agent.config.root / "src" / "user.py"
    ).read_text(encoding="utf-8")
    assert agent.plan is not None
    assert agent.get_view()["execution_mode"] == "autonomous"
    events = trace_events(agent.task_dir)
    assert any(event["event_type"] == "autonomous_plan" for event in events)
    assert not any(event["event_type"] == "approval" for event in events)


def test_autonomous_model_can_build_and_run_read_only_helper(
    workspace_factory, runtime_dir
):
    source_workspace = workspace_factory("e01_bugfix")
    correct_patch = """--- a/src/user.py
+++ b/src/user.py
@@ -1,4 +1,4 @@
 def validate_username(username: str | None) -> str:
-    if username is None:
+    if username is None or not username.strip():
         raise ValueError("username is required")
     return username
"""
    adapter = ScriptedModelAdapter(
        [
            call(
                "write_helper_script",
                {
                    "name": "inspect_user",
                    "source": (
                        "from pathlib import Path\n"
                        "print(Path('src/user.py').read_text(encoding='utf-8'))\n"
                    ),
                },
            ),
            call(
                "run_helper_script",
                {"name": "inspect_user", "args": [], "timeout_seconds": 5},
            ),
            call("apply_patch", {"patch": correct_patch}),
            call("run_validation", {}),
        ]
    )
    agent = TaskOrchestrator(
        workspace=str(source_workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
    )

    assert agent.run_until_pause() == TaskState.SUCCESS
    assert agent.total_helper_runs == 1
    tool_results = [
        event
        for event in trace_events(agent.task_dir)
        if event["event_type"] == "tool_result"
        and event["data"]["name"] == "run_helper_script"
    ]
    assert tool_results[0]["data"]["result"]["exit_code"] == 0
    assert "validate_username" in tool_results[0]["data"]["result"]["stdout"]


def test_autonomous_model_can_create_and_run_helper_in_one_call(
    workspace_factory, runtime_dir
):
    source_workspace = workspace_factory("e01_bugfix")
    correct_patch = """--- a/src/user.py
+++ b/src/user.py
@@ -1,4 +1,4 @@
 def validate_username(username: str | None) -> str:
-    if username is None:
+    if username is None or not username.strip():
         raise ValueError("username is required")
     return username
"""
    adapter = ScriptedModelAdapter(
        [
            call(
                "run_helper_script",
                {
                    "name": "inspect_inline",
                    "source": (
                        "from pathlib import Path\n"
                        "print(Path('src/user.py').read_text(encoding='utf-8'))\n"
                    ),
                },
            ),
            call("apply_patch", {"patch": correct_patch}),
            call("run_validation", {}),
        ]
    )
    agent = TaskOrchestrator(
        workspace=str(source_workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
    )

    assert agent.run_until_pause() == TaskState.SUCCESS
    helper_result = next(
        event
        for event in trace_events(agent.task_dir)
        if event["event_type"] == "tool_result"
        and event["data"]["name"] == "run_helper_script"
    )
    assert helper_result["data"]["result"]["script_write"]["name"] == (
        "inspect_inline.py"
    )


def test_helper_tools_are_hidden_after_run_budget_is_used(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e01_bugfix")
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=DemoModelAdapter(),
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
        max_helper_runs=1,
    )
    agent.run_helper_runs = 1

    names = {
        schema["function"]["name"] for schema in agent._available_tool_schemas()
    }
    assert "write_helper_script" not in names
    assert "run_helper_script" not in names


def test_context_checkpoint_surfaces_current_javascript_syntax_error(
    tmp_path, runtime_dir
):
    if not shutil.which("node"):
        return
    workspace = create_website_workspace("创建一个简单网站", parent_dir=tmp_path)
    (workspace / "app.js").write_text("const broken = ;\n", encoding="utf-8")
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text="创建一个简单网站",
        model=DemoModelAdapter(),
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
    )

    checkpoint = agent._workspace_checkpoint()
    assert "必须先修复" in checkpoint
    assert "SyntaxError" in checkpoint


def test_different_invalid_tool_reasons_do_not_share_circuit_count(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e01_bugfix")
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=DemoModelAdapter(),
        data_dir=runtime_dir,
    )
    first = ToolResult.failure("invalid_tool_call", "first reason")
    second = ToolResult.failure("invalid_tool_call", "second reason")

    assert agent._repeated_tool_error("write_helper_script", first) is False
    assert agent._repeated_tool_error("write_helper_script", second) is False
    assert second.data["same_error_count"] == 1


def test_different_exact_edit_candidates_do_not_share_circuit_count(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory("e01_bugfix")
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=DemoModelAdapter(),
        data_dir=runtime_dir,
    )
    first_call = NormalizedToolCall(
        call_id="edit-1",
        name="edit_file",
        arguments={"path": "src/user.py", "old_string": "missing one", "new_string": "x"},
    )
    second_call = NormalizedToolCall(
        call_id="edit-2",
        name="edit_file",
        arguments={"path": "src/user.py", "old_string": "missing two", "new_string": "y"},
    )
    first = ToolResult.failure("edit_not_found", "not found")
    second = ToolResult.failure("edit_not_found", "not found")

    assert agent._repeated_tool_error("edit_file", first, first_call) is False
    assert agent._repeated_tool_error("edit_file", second, second_call) is False
    assert second.data["same_error_count"] == 1


def test_autonomous_validation_without_code_change_cannot_succeed(
    workspace_factory, runtime_dir
):
    workspace = workspace_factory(
        "e01_bugfix",
        validation_command=[sys.executable, "-c", "raise SystemExit(0)"],
    )
    adapter = ScriptedModelAdapter(
        [
            call("run_validation", {}),
            call("run_validation", {}),
            call("run_validation", {}),
        ]
    )
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
    )

    assert agent.run_until_pause() == TaskState.FAILED
    assert agent.get_view()["last_error"]["code"] == "repeated_tool_call"
    assert agent.last_validation is not None and agent.last_validation.passed
    assert agent.snapshot.changed_files() == []


def test_zero_code_website_bootstrap_reaches_autonomous_success(
    tmp_path, runtime_dir
):
    task = "从零创建一个可离线使用的点击计数网站"
    source_workspace = create_website_workspace(task, parent_dir=tmp_path)
    product_patch = '''--- /dev/null
+++ b/index.html
@@ -0,0 +1,18 @@
+<!doctype html>
+<html lang="zh-CN">
+<head>
+  <meta charset="utf-8">
+  <meta name="viewport" content="width=device-width, initial-scale=1">
+  <title>点击计数器</title>
+  <link rel="stylesheet" href="styles.css">
+</head>
+<body>
+  <main class="card">
+    <p class="eyebrow">离线小工具</p>
+    <h1>点击计数器</h1>
+    <output id="count" aria-live="polite">0</output>
+    <button id="increment" type="button">点击 +1</button>
+    <button id="reset" type="button">重置</button>
+  </main>
+  <script src="app.js"></script>
+</body>
+</html>
--- /dev/null
+++ b/styles.css
@@ -0,0 +1,46 @@
+:root {
+  --ink: #15233b;
+  --paper: #fffaf0;
+  --accent: #ef5b5b;
+}
+* { box-sizing: border-box; }
+body {
+  margin: 0;
+  min-height: 100vh;
+  display: grid;
+  place-items: center;
+  color: var(--ink);
+  background: linear-gradient(135deg, #ccecff, var(--paper));
+  font-family: system-ui, sans-serif;
+}
+.card {
+  width: min(92vw, 32rem);
+  padding: 2.5rem;
+  border: 3px solid var(--ink);
+  border-radius: 1.5rem;
+  background: white;
+  box-shadow: 10px 10px 0 var(--ink);
+  text-align: center;
+}
+.eyebrow { letter-spacing: .18em; text-transform: uppercase; }
+output { display: block; margin: 1rem; font-size: 5rem; font-weight: 800; }
+button {
+  margin: .35rem;
+  padding: .8rem 1.2rem;
+  border: 2px solid var(--ink);
+  border-radius: 999px;
+  color: var(--ink);
+  background: var(--paper);
+  font: inherit;
+  font-weight: 700;
+  cursor: pointer;
+}
+button:hover { background: var(--accent); color: white; }
+button:focus-visible { outline: 4px solid #1c7ed6; outline-offset: 3px; }
+@media (max-width: 480px) {
+  .card { padding: 1.5rem; box-shadow: 6px 6px 0 var(--ink); }
+  output { font-size: 4rem; }
+}
+@media (prefers-reduced-motion: reduce) {
+  *, *::before, *::after { scroll-behavior: auto !important; }
+}
--- /dev/null
+++ b/app.js
@@ -0,0 +1,34 @@
+(() => {
+  "use strict";
+  const countOutput = document.querySelector("#count");
+  const incrementButton = document.querySelector("#increment");
+  const resetButton = document.querySelector("#reset");
+  const storageKey = "demo-click-counter";
+  let count = Number.parseInt(localStorage.getItem(storageKey) || "0", 10);
+
+  function normalizeCount(value) {
+    if (!Number.isFinite(value) || value < 0) return 0;
+    return Math.floor(value);
+  }
+
+  function render() {
+    count = normalizeCount(count);
+    countOutput.textContent = String(count);
+    countOutput.setAttribute("aria-label", `当前计数 ${count}`);
+    localStorage.setItem(storageKey, String(count));
+  }
+
+  function increment() {
+    count += 1;
+    render();
+  }
+
+  function reset() {
+    count = 0;
+    render();
+  }
+
+  incrementButton.addEventListener("click", increment);
+  resetButton.addEventListener("click", reset);
+  document.addEventListener("keydown", (event) => {
+    if (event.key === "Enter" || event.key === " ") increment();
+    if (event.key.toLowerCase() === "r") reset();
+  });
+  render();
+})();
'''
    adapter = ScriptedModelAdapter(
        [
            call("apply_patch", {"patch": product_patch}),
            call("run_validation", {}),
        ]
    )
    agent = TaskOrchestrator(
        workspace=str(source_workspace),
        task_text=task,
        model=adapter,
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
    )

    assert agent.run_until_pause() == TaskState.SUCCESS
    assert not (source_workspace / "index.html").exists()
    assert (agent.config.root / "index.html").is_file()
    assert agent.last_validation is not None
    assert agent.last_validation.passed


def test_write_file_change_is_tracked_and_can_finish_autonomous_task(
    workspace_factory, runtime_dir
):
    source_workspace = workspace_factory("e01_bugfix")
    corrected_source = '''# UNIQUE_CONTEXT_PAYLOAD
def validate_username(username: str | None) -> str:
    if username is None or not username.strip():
        raise ValueError("username is required")
    return username
'''
    adapter = ScriptedModelAdapter(
        [
            call(
                "write_file",
                {"path": "src/user.py", "content": corrected_source},
            ),
            *[
                call("list_files", {"directory": "", "max_results": limit})
                for limit in range(1, 7)
            ],
            call("run_validation", {}),
        ]
    )
    agent = TaskOrchestrator(
        workspace=str(source_workspace),
        task_text=E01_TASK,
        model=adapter,
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
    )

    assert agent.run_until_pause() == TaskState.SUCCESS
    assert "not username.strip()" not in (
        source_workspace / "src" / "user.py"
    ).read_text(encoding="utf-8")
    assert "not username.strip()" in (
        agent.config.root / "src" / "user.py"
    ).read_text(encoding="utf-8")
    assert agent.get_view()["changed_files"] == ["src/user.py"]
    second_request = json.dumps(adapter.requests[1]["messages"], ensure_ascii=False)
    assert "UNIQUE_CONTEXT_PAYLOAD" in second_request
    final_request = json.dumps(adapter.requests[-1]["messages"], ensure_ascii=False)
    assert "UNIQUE_CONTEXT_PAYLOAD" not in final_request
    assert "Harness Context Checkpoint" in final_request
    assert "HARNESS_COMPACTED" not in final_request


def test_twenty_game_site_can_be_built_in_append_batches(
    tmp_path, runtime_dir
):
    task = (
        "写一个小游戏网站，里面需要有20个童年的游戏，"
        "有一个需要是我的世界，需要直接在网页上就可以玩"
    )
    source_workspace = create_website_workspace(task, parent_dir=tmp_path)
    game_ids = [
        "minecraft",
        "snake",
        "tetris",
        "minesweeper",
        "sokoban",
        "game-2048",
        "gomoku",
        "othello",
        "shooter",
        "memory",
        "jump",
        "pinball",
        "maze",
        "sudoku",
        "tic-tac-toe",
        "breakout",
        "pacman",
        "tanks",
        "match-pairs",
        "pipes",
    ]
    manifest = [
        {
            "id": game_id,
            "name": "我的世界" if game_id == "minecraft" else "童年游戏 %s" % index,
            "instructions": "完成挑战并获得分数，可随时重新开始",
            "controls": ["点击", "方向键"],
        }
        for index, game_id in enumerate(game_ids, start=1)
    ]
    html = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>童年游戏厅</title><link rel="stylesheet" href="styles.css"></head>
<body><main><h1>童年游戏厅</h1><section id="lobby"><ul id="game-list"></ul></section>
<section id="play-area" hidden><button id="back-to-lobby">返回大厅</button>
<div id="game-stage"></div></section></main>
<script src="app.js"></script></body></html>
'''
    css = ''':root { --ink: #172554; --paper: #fff7ed; }
* { box-sizing: border-box; }
body { margin: 0; color: var(--ink); background: var(--paper); font-family: sans-serif; }
main { width: min(70rem, 94vw); margin: auto; padding: 2rem; }
button { padding: .8rem; cursor: pointer; }
button:focus-visible { outline: 3px solid #2563eb; }
@media (max-width: 600px) { main { padding: 1rem; } }
@media (prefers-reduced-motion: reduce) { * { animation: none !important; } }
'''
    core = '''"use strict";
const gameRegistry = new Map();
function registerGame(id, meta, factory) {
  gameRegistry.set(id, { meta: Object.assign({ id }, meta), factory });
}
function get(id) { return gameRegistry.get(id); }
function list() { return Array.from(gameRegistry.values()); }
window.GameHub = { registerGame, get, list };
const gameRoot = document.getElementById("game-stage");
const gameList = document.getElementById("game-list");
function enterGame(entry) {
  while (gameRoot.firstChild) gameRoot.removeChild(gameRoot.firstChild);
  document.getElementById("lobby").hidden = true;
  document.getElementById("play-area").hidden = false;
  entry.factory(gameRoot, { meta: entry.meta });
}
function renderLobby() {
  list().forEach((entry) => {
    const card = document.createElement("li");
    card.addEventListener("click", () => enterGame(entry));
    gameList.appendChild(card);
  });
}
document.addEventListener("DOMContentLoaded", renderLobby);
document.getElementById("back-to-lobby").addEventListener("click", () => {
  document.getElementById("lobby").hidden = false;
  document.getElementById("play-area").hidden = true;
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") gameRoot.dataset.state = "lobby";
});
function generateWorld() { return Array.from({ length: 64 }, (_, i) => i % 4); }
function breakBlock(world, index) { world[index] = 0; }
function placeBlock(world, index, block) { world[index] = block; }
const hotbar = ["grass", "stone", "wood"];
const canvas = document.createElement("canvas");
localStorage.setItem("childhood-world", JSON.stringify(generateWorld()));'''

    def game_source(game_id):
        return '''(function () {
const game = { id: %r };
    window.GameHub.registerGame(game.id, {
      id: game.id,
      name: game.id,
      instructions: "完成挑战",
      controls: "点击或方向键"
    }, function (container) {
  let score = 0;
  let running = true;
  const panel = document.createElement("section");
  const status = document.createElement("output");
  const action = document.createElement("button");
  action.type = "button";
  action.textContent = "开始挑战";
  function render() {
    status.textContent = "得分：" + score + "，状态：" + (running ? "进行中" : "结束");
    status.setAttribute("aria-label", "当前游戏得分 " + score);
  }
  function playStep() { if (running) { score += 1; render(); } }
  function onKey(event) { if (event.key.startsWith("Arrow")) playStep(); }
  action.addEventListener("click", playStep);
  document.addEventListener("keydown", onKey);
  panel.append(status, action);
  container.appendChild(panel);
  render();
  return {
    restart() { score = 0; running = true; render(); },
    destroy() { running = false; document.removeEventListener("keydown", onKey); }
  };
});
})();
''' % game_id

    responses = [
        call("write_file", {"path": "index.html", "content": html}),
        call("write_file", {"path": "styles.css", "content": css}),
        call(
            "write_file",
            {
                "path": "game-manifest.json",
                "content": json.dumps(manifest, ensure_ascii=False, indent=2),
            },
        ),
        call("write_file", {"path": "app.js", "content": core}),
    ]
    for offset in range(0, len(game_ids), 2):
        responses.append(
            call(
                "write_file",
                {
                    "path": "app.js",
                    "content": "".join(
                        game_source(game_id)
                        for game_id in game_ids[offset : offset + 2]
                    ),
                    "mode": "append",
                },
            )
        )
    responses.append(call("run_validation", {}))

    agent = TaskOrchestrator(
        workspace=str(source_workspace),
        task_text=task,
        model=ScriptedModelAdapter(responses),
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.AUTONOMOUS,
        max_turns=40,
    )

    assert agent.run_until_pause() == TaskState.SUCCESS
    assert agent.last_validation is not None and agent.last_validation.passed
    progress_results = [
        event["data"]["result"]["website_progress"]
        for event in trace_events(agent.task_dir)
        if event["event_type"] == "tool_result"
        and event["data"]["name"] == "write_file"
        and "website_progress" in event["data"]["result"]
    ]
    assert progress_results[-1]["registered_count"] == 20
    assert progress_results[-1]["missing_ids"] == []
    assert "window.GameHub" in (agent.config.root / "app.js").read_text(
        encoding="utf-8"
    )
