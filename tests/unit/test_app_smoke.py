from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from agent_harness.domain import ExecutionMode
from agent_harness.model_adapter import OpenAICompatibleAdapter
from agent_harness.orchestrator import TaskOrchestrator


APP_PATH = Path(__file__).resolve().parents[2] / "app.py"


def test_app_defaults_to_zero_code_website_flow():
    app = AppTest.from_file(str(APP_PATH)).run(timeout=15)

    assert not app.exception
    assert app.radio[0].value == "从零创建网站"
    assert app.selectbox[0].value == "自动执行"
    assert len(app.selectbox) == 1
    assert {item.label for item in app.text_input} >= {
        "接口地址",
        "模型 ID",
        "API Key",
    }
    start = next(
        item for item in app.button if item.label == "开始创建网站"
    )
    assert start.disabled
    assert any("无需准备代码目录" in item.value for item in app.success)


def test_existing_project_flow_still_exposes_workspace_validation():
    app = AppTest.from_file(str(APP_PATH)).run(timeout=15)
    app.radio[0].set_value("使用已有代码项目").run(timeout=15)

    assert not app.exception
    assert any(item.label == "校验工作区" for item in app.button)
    assert any(item.key == "workspace_path" for item in app.text_input)


def test_zero_code_website_start_requires_real_model_configuration():
    app = AppTest.from_file(str(APP_PATH)).run(timeout=15)
    next(item for item in app.text_input if item.label == "接口地址").set_value(
        "https://example.test/v1"
    )
    next(item for item in app.text_input if item.label == "模型 ID").set_value(
        "test-model"
    )
    next(item for item in app.text_input if item.label == "API Key").set_value(
        "test-key"
    )
    app.text_area[0].set_value("从零创建一个网站").run(timeout=15)

    assert not app.exception
    start = next(item for item in app.button if item.label == "开始创建网站")
    assert not start.disabled


def test_task_header_prioritizes_outcome_and_collapses_experiment_metrics(
    workspace_factory,
    runtime_dir,
):
    workspace = workspace_factory("e01_bugfix")
    agent = TaskOrchestrator(
        workspace=str(workspace),
        task_text="修复用户名校验",
        model=OpenAICompatibleAdapter(
            base_url="https://example.test/v1",
            api_key="test-key",
            model="test-model",
        ),
        data_dir=runtime_dir,
        execution_mode=ExecutionMode.SUPERVISED,
    )
    app = AppTest.from_file(str(APP_PATH)).run(timeout=15)
    app.session_state["agent"] = agent
    app.run(timeout=15)

    assert not app.exception
    metric_labels = [item.label for item in app.metric]
    assert metric_labels[:2] == ["执行时长", "验证结果"]
    assert metric_labels[2:] == [
        "模型轮次",
        "工具调用",
        "辅助分析",
        "Token 用量",
    ]
    assert any(item.label == "运行详情" for item in app.expander)
    assert not any(item.label == "API Key" for item in app.text_input)
    assert app.metric[0].value == "0 秒"
    assert app.metric[1].value == "尚未验证"
