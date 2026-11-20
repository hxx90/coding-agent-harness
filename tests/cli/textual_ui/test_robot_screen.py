from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button, Select, Static

from tests.conftest import build_test_vibe_app, build_test_vibe_config, wait_until
from vibe.app_server.models import (
    HardwareDeviceOutput,
    HardwareEvidenceOutput,
    HardwareSnapshotOutput,
)
from vibe.app_server.protocol import HardwareSetupResponse
from vibe.cli.commands import CommandRegistry
from vibe.cli.textual_ui.screens.robot import RobotScreen


def response(*, connected=False):
    return HardwareSetupResponse(
        devices=[
            HardwareDeviceOutput(
                device_id="panda",
                manufacturer="Robo",
                model="Panda",
                kind="robot_arm",
                transport="mcp_stdio",
                metadata={
                    "is_physical": False,
                    "environment": "Lift",
                    "has_viewer": True,
                    "records_video": True,
                },
            )
        ],
        model_name="Opus 5.5",
        model_configured=True,
        selection_confirmed=connected,
        selected_device_id="panda" if connected else None,
        snapshot=HardwareSnapshotOutput(
            device_id="panda",
            status="connected",
            sequence=2,
            evidence=[
                HardwareEvidenceOutput(
                    source_id="camera", modality="rgb", uri="file:///tmp/current.png"
                )
            ],
        )
        if connected
        else None,
    )


@pytest.mark.asyncio
async def test_robot_picker_only_connects_on_click_and_shows_evidence(tmp_path):
    app = build_test_vibe_app(config=build_test_vibe_config(theme="textual-dark"))
    setup = AsyncMock(side_effect=[response(), response(connected=True)])
    async with app.run_test(size=(120, 44)) as pilot:
        await app.push_screen(RobotScreen(setup))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RobotScreen)
        setup.assert_awaited_once_with(None, connect=False)
        screen.query_one(Select).value = "panda"
        await pilot.pause()
        assert not screen.query_one("#robot-connect", Button).disabled
        assert "Lift" in str(screen.query_one("#robot-capabilities", Static).render())
        await pilot.click("#robot-connect")
        await pilot.pause()
        setup.assert_awaited_with("panda", connect=True)
        assert not screen.query_one("#robot-image", Button).disabled
        assert "序号 2" in str(screen.query_one("#robot-observation", Static).render())
        assert "未发请求验证" in str(screen.query_one("#robot-status", Static).render())
        app.save_screenshot("robot-picker.svg", path=str(tmp_path))
        await pilot.click("#robot-close")
        assert not isinstance(app.screen, RobotScreen)


@pytest.mark.asyncio
async def test_empty_or_failed_discovery_does_not_enable_connect():
    app = build_test_vibe_app()
    setup = AsyncMock(return_value=HardwareSetupResponse(issues=["连接失败"]))
    async with app.run_test(size=(110, 40)) as pilot:
        await app.push_screen(RobotScreen(setup))
        await pilot.pause()
        assert app.screen.query_one("#robot-connect", Button).disabled
        assert "未发现机器人" in str(
            app.screen.query_one("#robot-status", Static).render()
        )
        await pilot.press("escape")


@pytest.mark.parametrize(
    "uri",
    [
        "https://host/current.png",
        "file://remote/tmp/a.png",
        "file:///tmp/a.sh",
        "file:relative.png",
    ],
)
def test_robot_image_rejects_remote_and_executable_paths(uri):
    assert RobotScreen._local_image(uri) is None


def test_robot_command_requires_idle():
    command = CommandRegistry().commands["robot"]
    assert command.handler == "_show_robot"
    assert not command.side_channel


@pytest.mark.asyncio
async def test_idle_escape_and_narrow_buttons():
    app = build_test_vibe_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await app.push_screen(
            RobotScreen(AsyncMock(return_value=response(connected=True)))
        )
        await pilot.pause()
        screen = app.screen
        screen.query_one("#robot-close", Button).focus()
        await pilot.pause(0.6)
        for button in screen.query(Button):
            assert (
                screen.find_widget(button).visible_region.width == button.region.width
            )
            assert (
                screen.find_widget(button).visible_region.height == button.region.height
            )
        await pilot.press("escape")
        assert not isinstance(app.screen, RobotScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("focus_before_resize", [False, True])
async def test_connected_robot_buttons_remain_reachable_after_resize(
    focus_before_resize,
):
    app = build_test_vibe_app()
    async with app.run_test(size=(120, 44)) as pilot:
        await app._show_robot()
        screen = app.screen
        assert isinstance(screen, RobotScreen)
        assert await wait_until(pilot, lambda: bool(screen._response.devices))
        screen.query_one(Select).value = "sim-arm-1"
        await pilot.pause()
        await pilot.click("#robot-connect")
        assert await wait_until(pilot, lambda: screen._response.selection_confirmed)
        if focus_before_resize:
            screen.query_one("#robot-close", Button).focus()
            await pilot.pause()
        await pilot.resize_terminal(80, 24)
        if not focus_before_resize:
            screen.query_one("#robot-close", Button).focus()
        await pilot.pause(0.6)
        for button in screen.query(Button):
            assert screen.find_widget(button).visible_region == button.region

        assert screen.focused is screen.query_one("#robot-close", Button)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: not isinstance(app.screen, RobotScreen))
