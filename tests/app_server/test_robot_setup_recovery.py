from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tests.conftest import build_test_agent_loop, build_test_vibe_app
from tests.stubs.app_server import (
    attach_test_app_server_session,
    build_test_app_server,
    legacy_backend,
)
from vibe.app_server.client import AppServerClient
from vibe.app_server.transport import memory_transport_pair
from vibe.cli.textual_ui.screens.robot import RobotScreen


@pytest_asyncio.fixture
async def setup_session():
    agent = build_test_agent_loop()
    ct, st = memory_transport_pair()
    server = build_test_app_server(agent, st)
    session = await attach_test_app_server_session(
        AppServerClient(ct, run_peer=server.serve)
    )
    try:
        yield agent, session, legacy_backend(server)
    finally:
        await session.close()
        await agent.aclose()


@pytest.mark.asyncio
async def test_clear_reselect_and_failed_refresh_keep_context_consistent(
    setup_session, monkeypatch
):
    agent, session, backend = setup_session
    await session.setup_hardware("sim-arm-1", connect=True)
    await session.clear_history()
    assert backend._selected_robot is None
    assert not any(
        "Operator selected robot" in (m.content or "") for m in agent.messages
    )
    result = await session.setup_hardware("sim-arm-1", connect=True)
    assert result.selection_confirmed
    messages = list(agent.messages)
    assert any(
        'Operator selected robot "sim-arm-1"' in (m.content or "") for m in messages
    )
    monkeypatch.setattr(
        agent.hardware_runtime, "discover", AsyncMock(side_effect=TimeoutError())
    )
    result = await session.setup_hardware()
    assert result.selected_device_id == backend._selected_robot == "sim-arm-1"
    assert not result.selection_confirmed
    assert result.issues
    assert list(agent.messages) == messages
    assert "sim-arm-1" in agent.hardware_runtime.connected_device_ids


@pytest.mark.asyncio
async def test_rpc_cancel_is_scoped_and_acknowledges_released_lifecycle(
    setup_session, monkeypatch
):
    agent, session, backend = setup_session
    begun = asyncio.Event()
    cancelled = asyncio.Event()
    original = agent.hardware_runtime.discover

    async def slow():
        begun.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(agent.hardware_runtime, "discover", slow)
    task = asyncio.create_task(session.setup_hardware(operation_id="current"))
    await begun.wait()
    assert not (await session.cancel_hardware_setup("stale")).settled
    assert backend.session.execution.active is not None
    assert not cancelled.is_set()
    assert (await session.cancel_hardware_setup("current")).settled
    assert cancelled.is_set()
    assert backend.session.execution.active is None
    assert (await task).cancelled
    monkeypatch.setattr(agent.hardware_runtime, "discover", original)
    assert (await session.setup_hardware()).devices


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_via", ["escape", "button"])
async def test_ui_cancel_reaches_server_before_returning(
    setup_session, monkeypatch, cancel_via
):
    agent, session, backend = setup_session
    begun = asyncio.Event()

    async def slow():
        begun.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(agent.hardware_runtime, "discover", slow)
    app = build_test_vibe_app()
    async with app.run_test(size=(120, 44)) as pilot:
        app.push_screen(
            RobotScreen(session.setup_hardware, cancel=session.cancel_hardware_setup)
        )
        await begun.wait()
        if cancel_via == "escape":
            await pilot.press("escape")
        else:
            await pilot.click("#robot-close")
        await pilot.pause()
        assert not isinstance(app.screen, RobotScreen)
        assert backend.session.execution.active is None
        assert not agent.hardware_runtime.connected_device_ids


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_connection_cleanup(setup_session, monkeypatch):
    agent, session, _ = setup_session
    runtime = agent.hardware_runtime
    await runtime.discover()
    adapter = runtime._device_adapters["sim-arm-1"]
    original_connect = adapter.connect
    original_cleanup = runtime._clear_failed_connection
    begun, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_connect(device_id):
        begun.set()
        await asyncio.Event().wait()

    async def delayed_cleanup(device_id, *, reconnecting):
        cleaning.set()
        await release.wait()
        await original_cleanup(device_id, reconnecting=reconnecting)

    monkeypatch.setattr(adapter, "connect", slow_connect)
    monkeypatch.setattr(runtime, "_clear_failed_connection", delayed_cleanup)
    setup = asyncio.create_task(
        session.setup_hardware("sim-arm-1", connect=True, operation_id="same")
    )
    try:
        await begun.wait()
        first = asyncio.create_task(session.cancel_hardware_setup("same"))
        await cleaning.wait()
        second = asyncio.create_task(session.cancel_hardware_setup("same"))
        await asyncio.sleep(0.02)
        assert not first.done() and not second.done()
        release.set()
        assert all(r.settled for r in await asyncio.gather(first, second))
        assert (await setup).cancelled
        assert not runtime._busy
        monkeypatch.setattr(adapter, "connect", original_connect)
        assert (await session.setup_hardware("sim-arm-1", connect=True)).snapshot
    finally:
        release.set()


@pytest.mark.asyncio
async def test_late_cancel_waits_for_selection_commit(setup_session, monkeypatch):
    agent, session, backend = setup_session
    begun, release = asyncio.Event(), asyncio.Event()
    original = agent._save_messages

    async def delayed_save(*args, **kwargs):
        begun.set()
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(agent, "_save_messages", delayed_save)
    setup = asyncio.create_task(
        session.setup_hardware("sim-arm-1", connect=True, operation_id="commit")
    )
    try:
        await begun.wait()
        cancel = asyncio.create_task(session.cancel_hardware_setup("commit"))
        await asyncio.sleep(0.02)
        assert not cancel.done()
        release.set()
        result, ack = await asyncio.gather(setup, cancel)
        assert ack.settled
        assert result.selected_device_id == backend._selected_robot == "sim-arm-1"
        assert any(
            'Operator selected robot "sim-arm-1"' in (m.content or "")
            for m in agent.messages
        )
        assert backend.session.execution.active is None
    finally:
        release.set()
