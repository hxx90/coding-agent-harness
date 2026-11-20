from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Static

from vibe.app_server.events import (
    HistoryEntryAdded,
    HistoryEntryUpdated,
    TurnCompleted,
    TurnStarted,
)
from vibe.app_server.models import (
    CompletedEffectState,
    EffectCallDisplay,
    EffectResultDisplay,
    HardwareEffectDetail,
    HardwareEffectInput,
    PublicEffectEntry,
    PublicEntryGenerationStatus,
    PublicError,
    PublicTurn,
    PublicTurnStatus,
)
from vibe.cli.textual_ui.widgets.hardware_progress import (
    HardwareProgress,
    HardwareProgressPanel,
    local_artifact_path,
)


def effect(
    name: str,
    output=None,
    *,
    action=None,
    code=None,
    state=None,
    device="panda",
    turn="turn-1",
):
    return PublicEffectEntry(
        id=name,
        session_id="session",
        turn_id=turn,
        created_at=1,
        updated_at=1,
        generation_status=PublicEntryGenerationStatus.COMPLETED
        if state is None
        else PublicEntryGenerationStatus.IN_PROGRESS,
        title=name,
        detail=HardwareEffectDetail(
            tool_name="robo",
            input=HardwareEffectInput(device_id=device, action=action, code=code),
            display=EffectCallDisplay(summary=name, status_text=name),
        ),
        state=state
        or CompletedEffectState(
            output=output, display=EffectResultDisplay(success=True, message="ok")
        ),
    )


def motion(reached: bool):
    return {
        "command_id": "move-1",
        "device_id": "panda",
        "status": "completed",
        "result": {
            "motion": {
                "reached": reached,
                "reason": "reached" if reached else "timeout",
                "error_max_m": 0.002 if reached else 0.04,
                "cycles": 40,
                "trace_uri": "file:///tmp/trace.jsonl",
            }
        },
    }


def verification(status="passed"):
    return {
        "report": {
            "criterion": "lift_object",
            "status": status,
            "summary": "Independent result",
            "checks": [],
        }
    }


def test_motion_success_is_not_task_success_or_cleanup():
    summary = HardwareProgress()
    summary.update(effect("move", {"receipt": motion(True)}, action="move_to"))
    assert "已到位" in summary.control
    assert "2.0 mm" in summary.control
    assert "40 周期" in summary.control
    assert "待重新验证" in summary.task
    assert summary.stop == "未确认"
    assert summary.lease == "未确认释放"


def test_completed_receipt_with_timeout_is_not_reached():
    summary = HardwareProgress()
    summary.update(effect("move", {"receipt": motion(False)}, action="move_to"))
    assert "未到位" in summary.control
    assert "timeout" in summary.failure


def test_verification_invalidation_cleanup_and_duplicate_events():
    summary = HardwareProgress()
    summary.update(effect("verify", verification()))
    assert "passed" in summary.task
    move = effect("move", {"receipt": motion(True)}, action="move_to")
    summary.update(move)
    summary.update(move)
    assert len(summary.effects) == 2
    assert "passed" not in summary.task
    summary.update(effect("verify-again", verification()))
    summary.update(
        effect(
            "stop",
            {
                "receipt": {
                    "device_id": "panda",
                    "acknowledged": True,
                    "message": "stopped",
                }
            },
        )
    )
    assert summary.stop == "已确认 ACK"
    assert summary.lease != "已释放"
    summary.update(effect("release", {"released": True}, action="release"))
    assert "passed" in summary.task
    assert summary.lease == "已释放"
    summary.update(effect("new-policy", state={"status": "running"}, code="observe()"))
    assert "passed" not in summary.task
    assert summary.stop == "未确认"


def test_other_device_does_not_inherit_task_success():
    summary = HardwareProgress()
    summary.update(effect("verify", verification()))
    summary.update(
        effect("connect", {"action": "connect"}, action="connect", device="other")
    )
    assert "passed" not in summary.task


def test_cancel_failure_and_malformed_output_are_visible():
    summary = HardwareProgress()
    summary.update(
        effect(
            "bad",
            state={
                "status": "failed",
                "error": {"message": "statement_limit line=7"},
                "display": {"success": False, "message": "failed"},
            },
        )
    )
    assert "line=7" in summary.failure
    summary.update(
        effect("cancel", state={"status": "cancelled", "reason": "user cancelled"})
    )
    assert summary.phase == "已取消"
    assert summary.stop == "未确认"
    summary.update(effect("malformed", {"unexpected": True}))
    assert "无法解析" in summary.failure


@pytest.mark.parametrize(
    "uri",
    [
        "https://host/a.mp4",
        "file://remote/tmp/a.mp4",
        "file:relative.mp4",
        "file:///tmp/a.app",
        "file:///tmp/a.sh",
    ],
)
def test_artifact_opener_rejects_remote_and_executable_targets(uri):
    assert local_artifact_path(uri) is None


def test_artifact_path_handles_spaces():
    assert str(local_artifact_path("file:///tmp/a%20b.mp4")) == "/tmp/a b.mp4"


class PanelApp(App):
    def compose(self) -> ComposeResult:
        yield HardwareProgressPanel()


@pytest.mark.asyncio
async def test_panel_live_updates_buttons_and_turn_reset(tmp_path):
    app = PanelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        panel = app.query_one(HardwareProgressPanel)
        assert not panel.display
        running = effect(
            "policy",
            state={
                "status": "running",
                "output_text": "#5 command_started · move_to\n",
            },
            code="observe()",
        )
        panel.handle_event(HistoryEntryAdded(running))
        assert panel.display
        assert "move_to" in panel.progress.phase
        done = effect(
            "policy",
            {
                "snapshot": {
                    "device_id": "panda",
                    "status": "armed",
                    "sequence": 1,
                    "state": {"recording_uri": (tmp_path / "demo.mp4").as_uri()},
                },
                "receipt": motion(False),
            },
            code="observe()",
        )
        panel.handle_event(HistoryEntryUpdated(previous=running, entry=done, patch=[]))
        assert not panel.query_one("#hardware-video", Button).disabled
        assert "未到位" in panel.progress.control
        await pilot.pause()
        assert panel.query_one("#hardware-details", Button).content_region.height >= 1
        await pilot.click("#hardware-details")
        assert panel.query_one(".hardware-details", Static).display
        assert "timeout" in str(panel.query_one(".hardware-details", Static).render())
        panel.handle_event(
            TurnCompleted(
                PublicTurn(
                    id="turn-1",
                    session_id="session",
                    status=PublicTurnStatus.COMPLETED,
                    started_at=1,
                )
            )
        )
        assert "本轮已结束" in panel.progress.phase
        assert "passed" not in panel.progress.task
        panel.handle_event(
            TurnStarted(
                PublicTurn(
                    id="turn-2",
                    session_id="session",
                    status=PublicTurnStatus.IN_PROGRESS,
                    started_at=2,
                )
            )
        )
        assert not panel.display
        assert not panel.progress.artifacts
        assert not panel.progress.effects


@pytest.mark.asyncio
async def test_opener_uses_argument_vector_without_shell(tmp_path):
    path = tmp_path / "video with spaces.mp4"
    path.touch()
    app = PanelApp()
    async with app.run_test():
        process = AsyncMock()
        process.returncode = 0
        with patch(
            "vibe.cli.textual_ui.widgets.hardware_progress.asyncio.create_subprocess_exec",
            return_value=process,
        ) as spawn:
            await app.query_one(HardwareProgressPanel)._open_artifact(path.as_uri())
        assert spawn.call_args.args[1] == str(path)
        assert "shell" not in spawn.call_args.kwargs


@pytest.mark.asyncio
async def test_real_cli_mounts_panel_and_routes_public_events():
    from tests.conftest import build_test_vibe_app

    app = build_test_vibe_app()
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause(0.1)
        entry = effect("verify", verification())
        await app._handle_turn_event(HistoryEntryAdded(entry))
        await pilot.pause()
        panel = app.query_one(HardwareProgressPanel)
        assert panel.display
        assert "passed" in panel.progress.task
        assert panel.region.height >= 5
        assert panel.region.bottom <= app.size.height
        await app._reset_message_widgets()
        assert not panel.display


def test_policy_with_geometry_metadata_keeps_structured_results():
    from vibe.app_server._tool_projection import project_effect_output_value
    from vibe.utils.tool_presentation import ToolEffectKind

    policy = {
        "geometry_library": "off",
        "policy_id": "policy",
        "source": "observe()",
        "source_sha256": "a" * 64,
        "source_uri": "file:///tmp/policy.py",
        "status": "completed",
        "steps": [],
        "receipts": [motion(True)],
        "snapshot": {
            "device_id": "panda",
            "sequence": 1,
            "status": "armed",
            "state": {"recording_uri": "file:///tmp/video.mp4"},
        },
        "report": verification()["report"],
        "task_outcome": "passed",
    }
    projected = project_effect_output_value(ToolEffectKind.HARDWARE, {"policy": policy})
    assert projected is not None
    summary = HardwareProgress()
    summary.update(effect("policy", projected, code="observe()"))
    assert "passed" in summary.task
    assert summary.artifacts["video"] == "file:///tmp/video.mp4"
    assert summary.artifacts["source"] == "file:///tmp/policy.py"


@pytest.mark.asyncio
async def test_turn_error_is_visible_in_failure_details():
    app = PanelApp()
    async with app.run_test(size=(120, 35)):
        panel = app.query_one(HardwareProgressPanel)
        panel.handle_event(HistoryEntryAdded(effect("connect", {"action": "connect"})))
        panel.handle_event(
            TurnCompleted(
                PublicTurn(
                    id="turn-1",
                    session_id="session",
                    status=PublicTurnStatus.FAILED,
                    started_at=1,
                    error=PublicError(
                        message="truncated:length", code="internal_error"
                    ),
                )
            )
        )
        assert "truncated:length" in str(
            panel.query_one(".hardware-details", Static).render()
        )


@pytest.mark.asyncio
async def test_recorded_failure_replay_in_real_cli(tmp_path):
    import json
    from pathlib import Path

    from tests.conftest import build_test_vibe_app, build_test_vibe_config
    from vibe.app_server._hardware_cleanup import project_hardware_cleanup
    from vibe.app_server.events import SessionSnapshot

    recorded = json.loads(
        (
            Path(__file__).parents[2] / "app_server/data/robo_truncated_turn.json"
        ).read_text()
    )
    turn = PublicTurn.model_validate(recorded["turn"])
    turn.hardware_cleanup = project_hardware_cleanup(recorded["cleanup"])
    # Round-trip the actual public wire shape before rendering.
    turn = PublicTurn.model_validate_json(turn.model_dump_json())
    entries = [
        PublicEffectEntry.model_validate(x) for x in recorded["hardware_effects"]
    ]
    app = build_test_vibe_app(config=build_test_vibe_config(theme="textual-dark"))
    app.title = "Robo · recorded failure replay (no model)"
    async with app.run_test(size=(120, 40)) as pilot:
        for entry in entries:
            await app._handle_turn_event(HistoryEntryAdded(entry))
        await app._handle_turn_event(TurnCompleted(turn))
        await app._complete_unsolicited_turn(TurnCompleted(turn))
        panel = app.query_one(HardwareProgressPanel)
        await pilot.pause()
        summary = str(panel.query_one(".hardware-summary", Static).render())
        assert "系统兜底" in summary and "已确认 ACK" in summary
        assert "无租约，无需释放" in summary
        assert panel.progress.stop == "未确认"  # Actor never stopped.
        assert panel.progress.task == "未独立验证"  # Fallback isn't actor success.
        await pilot.click("#hardware-details")
        await pilot.pause()
        detail = str(panel.query_one(".hardware-details", Static).render())
        assert "truncated:length" in detail
        assert "兜底独立验证" in detail and "stack_objects：failed" in detail
        assert "暂无已记录失败" not in detail
        assert panel.region.bottom <= app.size.height
        app.save_screenshot("recorded-failure.svg", path=str(tmp_path))
        (tmp_path / "replay-result.json").write_text(
            json.dumps(
                {
                    "summary": summary,
                    "details": detail,
                    "model_calls": 0,
                    "robot_actions": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        # A refreshed snapshot must reconstruct the same evidence.
        state = app.app_server.state.model_copy(
            update={"turns": [turn], "history": entries}
        )
        panel.handle_event(SessionSnapshot(state))
        assert str(panel.query_one(".hardware-summary", Static).render()) == summary
        assert str(panel.query_one(".hardware-details", Static).render()) == detail
        panel.handle_event(
            TurnStarted(
                PublicTurn(
                    id="next",
                    session_id=turn.session_id,
                    started_at=2,
                    status=PublicTurnStatus.IN_PROGRESS,
                )
            )
        )
        assert panel.cleanup is None and panel.turn_error == ""
        assert not panel.display


@pytest.mark.asyncio
async def test_cleanup_failure_and_multiple_devices_do_not_invent_success():
    from vibe.app_server._hardware_cleanup import project_hardware_cleanup

    app = PanelApp()
    async with app.run_test():
        panel = app.query_one(HardwareProgressPanel)
        panel.reset("turn-1")
        turn = PublicTurn(
            id="turn-1",
            session_id="session",
            started_at=1,
            status=PublicTurnStatus.INTERRUPTED,
        )
        turn.hardware_cleanup = project_hardware_cleanup({
            "error": "cleanup_timeout",
            "devices": {
                "panda": {
                    "stop": {"error": "TimeoutError"},
                    "release": {"error": "RuntimeError"},
                },
                "other": {
                    "stop": {
                        "device_id": "other",
                        "acknowledged": False,
                        "message": "not stopped",
                    }
                },
            },
        })
        panel.handle_event(TurnCompleted(turn))
        detail = str(panel.query_one(".hardware-details", Static).render())
        assert "cleanup_timeout" in detail and "TimeoutError" in detail
        assert "未确认 ACK" in detail and "未确认释放" in detail
        assert "已确认 ACK" not in detail and "已释放" not in detail
        assert "panda" in detail and "other" in detail
        assert panel.progress.task == "未独立验证"
