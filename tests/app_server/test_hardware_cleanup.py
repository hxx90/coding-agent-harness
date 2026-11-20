from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from tests.mock.utils import mock_llm_chunk
from tests.stubs.app_server import attach_test_app_server_session, build_test_app_server
from tests.stubs.fake_backend import FakeBackend
from vibe.app_server.client import AppServerClient
from vibe.app_server.session import AppServerTurnError
from vibe.app_server.transport import memory_transport_pair
from vibe.core.config.models import GenerationConfig


@pytest.mark.asyncio
async def test_failed_generation_publishes_cleanup_over_serialized_turn(monkeypatch):
    recorded = json.loads(
        (Path(__file__).parent / "data/robo_truncated_turn.json").read_text()
    )
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            generation=GenerationConfig(enabled=True, limit_tokens=False)
        ),
        backend=FakeBackend(mock_llm_chunk(content="incomplete", stop_reason="length")),
    )
    cleanup = AsyncMock(return_value=recorded["cleanup"])
    monkeypatch.setattr(agent.hardware_runtime, "bounded_cleanup", cleanup)
    client_transport, server_transport = memory_transport_pair()
    server = build_test_app_server(agent, server_transport)
    session = await attach_test_app_server_session(
        AppServerClient(client_transport, run_peer=server.serve)
    )
    try:
        events = []
        with pytest.raises(AppServerTurnError, match="truncated:length"):
            async for event in session.act("Replay generation failure"):
                events.append(event)
        turn = session.state.latest_turn
        assert turn is not None
        assert turn.error is not None and turn.error.message == "truncated:length"
        result = getattr(turn, "hardware_cleanup", None)
        assert result is not None
        assert result.devices[0].stop is not None
        assert result.devices[0].stop.acknowledged is True
        assert result.devices[0].release == "no_lease"
        assert result.devices[0].verification is not None
        assert result.devices[0].verification.status == "failed"
        refreshed = session.state.latest_turn
        assert refreshed is not None and refreshed.hardware_cleanup == result
        cleanup.assert_awaited_once()
    finally:
        await session.close()
        await agent.aclose()


def test_cleanup_projection_preserves_failures_and_rejects_mismatched_receipts():
    from vibe.app_server._hardware_cleanup import project_hardware_cleanup

    result = project_hardware_cleanup({
        "devices": {
            "one": {
                "stop": {
                    "device_id": "other",
                    "acknowledged": True,
                    "message": "wrong device",
                },
                "release": False,
            },
            "two": {
                "verify": {"future_field": True},
                "stop": {"error": "TimeoutError"},
                "release": True,
            },
        },
        "error": "cleanup_timeout",
    })
    assert result.error == "cleanup_timeout"
    assert result.devices[0].stop is None
    assert result.devices[0].errors["stop"] == "device_mismatch"
    assert result.devices[0].release == "unconfirmed"
    assert result.devices[1].errors == {
        "verify": "invalid_cleanup_receipt",
        "stop": "TimeoutError",
    }
    assert result.devices[1].release == "released"


def test_older_turn_without_cleanup_remains_readable():
    from vibe.app_server.models import PublicTurn

    recorded = json.loads(
        (Path(__file__).parent / "data/robo_truncated_turn.json").read_text()
    )
    assert PublicTurn.model_validate(recorded["turn"]).hardware_cleanup is None


@pytest.mark.asyncio
async def test_cancelled_turn_still_delivers_cleanup(monkeypatch):
    import asyncio

    from vibe.app_server.models import PublicTurnStatus
    from vibe.core.types import AssistantEvent

    agent = build_test_agent_loop(
        config=build_test_vibe_config(generation=GenerationConfig(enabled=True))
    )
    started = asyncio.Event()

    async def blocked_conversation(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
        yield AssistantEvent(content="never reached")

    monkeypatch.setattr(agent, "_conversation_loop", blocked_conversation)
    cleanup = AsyncMock(
        return_value={
            "devices": {
                "panda": {
                    "stop": {
                        "device_id": "panda",
                        "acknowledged": True,
                        "message": "stopped",
                    },
                    "release": True,
                }
            }
        }
    )
    monkeypatch.setattr(agent.hardware_runtime, "bounded_cleanup", cleanup)
    client_transport, server_transport = memory_transport_pair()
    server = build_test_app_server(agent, server_transport)
    session = await attach_test_app_server_session(
        AppServerClient(client_transport, run_peer=server.serve)
    )

    async def consume():
        return [event async for event in session.act("cancel this turn")]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await session.interrupt()
        await asyncio.wait_for(task, timeout=2)
        turn = session.state.latest_turn
        assert turn is not None and turn.status == PublicTurnStatus.INTERRUPTED
        assert turn.hardware_cleanup is not None
        assert turn.hardware_cleanup.devices[0].stop is not None
        assert turn.hardware_cleanup.devices[0].stop.acknowledged is True
        assert turn.hardware_cleanup.devices[0].release == "released"
        cleanup.assert_awaited_once()
    finally:
        task.cancel()
        await session.close()
        await agent.aclose()
