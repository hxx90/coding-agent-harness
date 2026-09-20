from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from textual.binding import Binding
from textual.content import Content
from textual.widgets import Static

from vibe.app_server.models import EffectCallDisplay, HardwareEffectDetail
from vibe.cli.textual_ui.app import VibeApp
from vibe.cli.textual_ui.widgets.tool_widgets import (
    HardwareResultWidget,
    get_result_widget,
)


def _rendered_lines(widget: HardwareResultWidget) -> str:
    child = next(child for child in widget.compose() if isinstance(child, Static))
    rendered = child.render()
    assert isinstance(rendered, Content)
    return rendered.plain


def test_cli_exposes_a_priority_emergency_stop_shortcut() -> None:
    binding = cast(
        Binding,
        next(
            binding
            for binding in VibeApp.BINDINGS
            if getattr(binding, "action", None) == "emergency_stop"
        ),
    )

    assert binding.key == "ctrl+shift+x"
    assert binding.priority is True


def test_hardware_result_widget_shows_observation_telemetry_and_faults() -> None:
    detail = HardwareEffectDetail(
        tool_name="robo_observe",
        display=EffectCallDisplay(summary="Observing", status_text="Observing"),
    )
    widget = get_result_widget(
        detail,
        {
            "snapshot": {
                "device_id": "mujoco-panda-1",
                "status": "armed",
                "sequence": 12,
                "state": {"mode": "armed", "end_effector_position": [0.1, 0.2, 0.3]},
                "faults": ["joint_limit"],
                "evidence": [
                    {"source_id": "agentview", "uri": "file:///tmp/frame.png"}
                ],
            },
            "trace": {
                "run_id": "run-1",
                "events": [
                    {
                        "sequence": 9,
                        "kind": "command_failed",
                        "device_id": "mujoco-panda-1",
                    }
                ],
            },
        },
        True,
        "Observed",
    )

    assert isinstance(widget, HardwareResultWidget)
    rendered = _rendered_lines(widget)
    assert "observation: mujoco-panda-1 · armed · sequence 12" in rendered
    assert "end_effector_position: [0.1, 0.2, 0.3]" in rendered
    assert "faults: joint_limit" in rendered
    assert "evidence: agentview · file:///tmp/frame.png" in rendered
    assert "#9 command_failed · mujoco-panda-1" in rendered


def test_hardware_result_widget_shows_verification_checks() -> None:
    detail = HardwareEffectDetail(
        tool_name="robo_verify",
        display=EffectCallDisplay(summary="Verifying", status_text="Verifying"),
    )
    widget = get_result_widget(
        detail,
        {
            "report": {
                "criterion": "lift_object",
                "status": "failed",
                "summary": "Object was not lifted.",
                "checks": [
                    {
                        "name": "object_lifted",
                        "status": "failed",
                        "observed": False,
                        "expected": "cube above table",
                    }
                ],
                "evidence": [
                    {
                        "source_id": "agentview",
                        "modality": "rgb",
                        "captured_at": datetime.now(UTC).isoformat(),
                        "sequence": 13,
                        "mime_type": "image/png",
                        "uri": "file:///tmp/verification.png",
                    }
                ],
            }
        },
        False,
        "Verification failed",
    )

    assert isinstance(widget, HardwareResultWidget)
    rendered = _rendered_lines(widget)
    assert "verification: lift_object · failed" in rendered
    assert "Object was not lifted." in rendered
    assert "check: object_lifted · failed" in rendered
    assert "evidence: agentview · file:///tmp/verification.png" in rendered


def test_hardware_result_widget_shows_discovered_devices_and_verification_gate() -> (
    None
):
    detail = HardwareEffectDetail(
        tool_name="robo_devices",
        display=EffectCallDisplay(summary="Discovering", status_text="Discovering"),
    )
    widget = get_result_widget(
        detail,
        {
            "devices": [
                {
                    "device_id": "mujoco-panda-1",
                    "manufacturer": "Franka Emika",
                    "model": "Panda",
                    "kind": "robot_arm",
                    "transport": "simulator",
                }
            ],
            "task_outcome": "not_verified",
        },
        True,
        "Discovered",
    )

    assert isinstance(widget, HardwareResultWidget)
    rendered = _rendered_lines(widget)
    assert (
        "device: mujoco-panda-1 · Franka Emika Panda · robot_arm/simulator" in rendered
    )
    assert "task: not_verified" in rendered


def test_hardware_result_widget_shows_code_policy_and_step_outputs() -> None:
    detail = HardwareEffectDetail(
        tool_name="robo_run_policy",
        display=EffectCallDisplay(summary="Running policy", status_text="Running"),
    )
    widget = get_result_widget(
        detail,
        {
            "policy": {
                "policy_id": "policy-1",
                "source": 'before = observe()\nhome()\nverify("at_home")',
                "source_sha256": "a" * 64,
                "source_uri": "file:///tmp/policy-1.py",
                "status": "completed",
                "steps": [
                    {
                        "sequence": 1,
                        "kind": "observation",
                        "name": "observe",
                        "line": 1,
                        "output": {"state": {"mode": "armed"}},
                    },
                    {
                        "sequence": 2,
                        "kind": "action",
                        "name": "home",
                        "line": 2,
                        "output": {"status": "completed"},
                    },
                ],
                "stdout": ["passed"],
                "receipts": [],
                "snapshot": {
                    "device_id": "sim-arm-1",
                    "status": "armed",
                    "sequence": 2,
                },
                "task_outcome": "passed",
            }
        },
        True,
        "Policy passed",
    )

    assert isinstance(widget, HardwareResultWidget)
    rendered = _rendered_lines(widget)
    assert "policy: completed · outcome passed · file:///tmp/policy-1.py" in rendered
    assert "before = observe()" in rendered
    assert "#1 line 1 · observation · observe" in rendered
    assert "output: {'state': {'mode': 'armed'}}" in rendered
    assert "#2 line 2 · action · home" in rendered
    assert "stdout: passed" in rendered
