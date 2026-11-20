from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.conftest import build_test_agent_loop
from tests.stubs.app_server import (
    attach_test_app_server_session,
    build_test_app_server,
    legacy_backend,
)
from tests.stubs.fake_backend import FakeBackend
from vibe.app_server._execution import SessionExecutionKind
from vibe.app_server.client import AppServerClient, AppServerResponseError
from vibe.app_server.protocol import ProtocolErrorCode
from vibe.app_server.transport import memory_transport_pair


@pytest.mark.asyncio
async def test_robot_setup_connects_without_model_arm_lease_or_scene_reset(monkeypatch):
    backend = FakeBackend()
    agent = build_test_agent_loop(backend=backend)
    client_transport, server_transport = memory_transport_pair()
    server = build_test_app_server(agent, server_transport)
    session = await attach_test_app_server_session(
        AppServerClient(client_transport, run_peer=server.serve)
    )
    try:
        listed = await session.setup_hardware()
        assert any(d.device_id == "sim-arm-1" for d in listed.devices)
        assert not agent.hardware_runtime.connected_device_ids
        assert not listed.model_connection_tested
        arm = AsyncMock()
        acquire = AsyncMock()
        execute = AsyncMock()
        monkeypatch.setattr(agent.hardware_runtime, "arm", arm)
        monkeypatch.setattr(agent.hardware_runtime, "acquire_lease", acquire)
        monkeypatch.setattr(agent.hardware_runtime, "execute", execute)
        connected = await session.setup_hardware("sim-arm-1", connect=True)
        assert connected.selected_device_id == "sim-arm-1"
        assert connected.snapshot is not None
        assert connected.snapshot.status != "armed"
        assert any(
            'Operator selected robot "sim-arm-1"' in (m.content or "")
            for m in agent.messages
        )
        size = len(agent.messages)
        connect = AsyncMock(side_effect=AssertionError("must not reconnect"))
        monkeypatch.setattr(agent.hardware_runtime, "connect", connect)
        refreshed = await session.setup_hardware("sim-arm-1", connect=True)
        assert refreshed.snapshot is not None
        assert refreshed.snapshot.observed_at is not None
        assert connected.snapshot.observed_at is not None
        assert refreshed.snapshot.observed_at > connected.snapshot.observed_at
        assert len(agent.messages) == size
        connect.assert_not_awaited()
        arm.assert_not_awaited()
        acquire.assert_not_awaited()
        execute.assert_not_awaited()
        assert backend.requests_messages == []
        missing = await session.setup_hardware("not-a-robot", connect=True)
        assert missing.issues and missing.selected_device_id == "sim-arm-1"
        assert not missing.snapshot
        execution = legacy_backend(server).session.execution
        held = execution.begin(SessionExecutionKind.TURN, "active-task")
        try:
            with pytest.raises(AppServerResponseError) as error:
                await session.setup_hardware("sim-arm-1", connect=True)
            assert error.value.error.code == ProtocolErrorCode.CONFLICT
        finally:
            execution.finish(held)
    finally:
        await session.close()
        await agent.aclose()


@pytest.mark.asyncio
async def test_setup_error_does_not_expose_transport_credentials(monkeypatch):
    from vibe.app_server._robot_setup import setup_robot
    from vibe.app_server.protocol import HardwareSetupParams

    agent = build_test_agent_loop()
    monkeypatch.setattr(
        agent.hardware_runtime,
        "discover",
        AsyncMock(side_effect=RuntimeError("secret-value")),
    )
    try:
        result = await setup_robot(agent, HardwareSetupParams(session_id="test"), None)
        assert result.issues
        assert "secret-value" not in result.model_dump_json()
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_model_preflight_failure_still_allows_robot_inspection(monkeypatch):
    from vibe.app_server._robot_setup import setup_robot
    from vibe.app_server.protocol import HardwareSetupParams

    agent = build_test_agent_loop()

    def missing_credentials(_self):
        raise RuntimeError("secret-value")

    monkeypatch.setattr(
        type(agent.config), "require_active_provider_api_key", missing_credentials
    )
    try:
        result = await setup_robot(
            agent,
            HardwareSetupParams(session_id="test", device_id="sim-arm-1", connect=True),
            None,
        )
        assert not result.model_configured
        assert not result.model_connection_tested
        assert result.snapshot is not None
        assert result.issues
        assert "secret-value" not in result.model_dump_json()
    finally:
        await agent.aclose()
