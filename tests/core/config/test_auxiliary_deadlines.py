from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.conftest import build_test_vibe_config
from vibe.core.config import admin_config
from vibe.core.experiments import session
from vibe.core.experiments.active import ExperimentSurface


@pytest.mark.asyncio
async def test_admin_fetch_caps_whole_request_and_can_retry(monkeypatch):
    monkeypatch.setattr(
        admin_config, "MANAGED_CONFIG_TOTAL_TIMEOUT", 0.02, raising=False
    )
    cancelled = asyncio.Event()

    async def stalled(*args):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(admin_config, "_fetch_response", stalled)
    async with asyncio.timeout(0.2):
        result = await admin_config.fetch_managed_config("http://test", "test-key")
    assert result.config is None
    assert result.error and "timed out" in result.error
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_plan_metadata_has_total_deadline(monkeypatch):
    monkeypatch.setattr(session, "EXPERIMENT_IDENTITY_TIMEOUT_S", 0.02)
    cancelled = asyncio.Event()

    async def stalled(**kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async with asyncio.timeout(0.2):
        attributes, _ = await session._fetch_plan_attributes(
            config=build_test_vibe_config(),
            launch_context=None,
            resolve_identity=stalled,
            resolve_whoami=AsyncMock(return_value=None),
            harness=ExperimentSurface.LEGACY,
        )
    assert cancelled.is_set()
    assert attributes is not None
    assert attributes.userId is None


@pytest.mark.asyncio
async def test_admin_cancellation_is_not_swallowed(monkeypatch):
    started = asyncio.Event()

    async def stalled(*args):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(admin_config, "_fetch_response", stalled)
    task = asyncio.create_task(
        admin_config.fetch_managed_config("http://test", "test-key")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_admin_deadline_includes_retry_backoff(monkeypatch, respx_mock):
    import httpx

    monkeypatch.setattr(admin_config, "MANAGED_CONFIG_TOTAL_TIMEOUT", 0.02)
    route = respx_mock.get("http://test" + admin_config.MANAGED_CONFIG_PATH).mock(
        side_effect=httpx.ConnectError("offline")
    )
    async with asyncio.timeout(0.2):
        result = await admin_config.fetch_managed_config("http://test", "test-key")
    assert result.error and "timed out" in result.error
    assert route.call_count == 1
    route.mock(return_value=httpx.Response(200, json={"state": "disabled"}))
    assert (
        await admin_config.fetch_managed_config("http://test", "test-key")
    ).error is None


@pytest.mark.asyncio
async def test_identity_deadline_and_retry(monkeypatch):
    from vibe.core import identity

    monkeypatch.setattr(identity, "_IDENTITY_TIMEOUT", 0.02)
    cancelled = asyncio.Event()

    async def stalled(**kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    gateway = AsyncMock()
    gateway.read.side_effect = stalled
    async with asyncio.timeout(0.2):
        assert (
            await identity.fetch_identity(
                base_url="http://test", api_key="test-key", gateway=gateway
            )
            is None
        )
    assert cancelled.is_set()
    gateway.read.side_effect = None
    gateway.read.return_value = identity.IdentityResult(id="user")
    result = await identity.fetch_identity(
        base_url="http://test", api_key="test-key", gateway=gateway
    )
    assert result is not None and result.id == "user"
