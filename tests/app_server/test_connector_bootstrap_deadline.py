"""A stalled optional Connector service must not hold startup indefinitely."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx

from tests.conftest import build_test_vibe_config
from tests.stubs.fake_config_orchestrator import FakeConfigOrchestrator
from vibe.app_server import connector_catalog
from vibe.core.tools.connectors import connector_registry


def _clients(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setattr(connector_registry, "_BOOTSTRAP_TIMEOUT", 0.05)
    monkeypatch.setattr(connector_catalog, "_BOOTSTRAP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(connector_registry, "_BOOTSTRAP_REFRESH_TIMEOUT", 0.2)
    monkeypatch.setattr(connector_catalog, "_BOOTSTRAP_REFRESH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(connector_registry, "_read_bootstrap_cache_entries", lambda: {})
    monkeypatch.setattr(
        connector_registry, "_write_bootstrap_cache_entries", lambda _entries: None
    )
    return (
        connector_registry.ConnectorRegistry(api_key="test-key"),
        connector_catalog.ConnectorCatalogService(
            implicit_source_enabled=False, cache_path=tmp_path / "connectors.json"
        ),
        FakeConfigOrchestrator(build_test_vibe_config(enable_connectors=True)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["legacy", "catalog"])
@pytest.mark.parametrize("force_refresh", [False, True])
async def test_stalled_bootstrap_has_total_deadline_and_can_retry(
    backend: str, force_refresh: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry, service, orchestrator = _clients(monkeypatch, tmp_path)
    monkeypatch.setattr(connector_registry, "_BOOTSTRAP_REFRESH_TIMEOUT", 0.05)
    monkeypatch.setattr(connector_catalog, "_BOOTSTRAP_REFRESH_TIMEOUT_SECONDS", 0.05)
    cancelled = asyncio.Event()
    attempts = 0

    async def stalled(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        raise AssertionError("The stalled request must be cancelled")

    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=r".*/v1/connectors/bootstrap").mock(
            side_effect=stalled
        )
        # The outer guard only makes the regression fast. Production must return
        # through its own timeout/fallback path before this guard fires.
        async with asyncio.timeout(0.5):
            if backend == "legacy":
                assert await registry.get_tools_async(force_refresh=force_refresh) == {}
                assert "timed out" in (registry.bootstrap_error() or "")
            else:
                with pytest.raises(
                    connector_catalog.ConnectorCatalogUnavailableError,
                    match="timed out",
                ):
                    await service.resolve_catalog(
                        orchestrator, force_refresh=force_refresh
                    )
                assert service.read_catalog(orchestrator).catalog is None
        assert cancelled.is_set()
        assert attempts == 1

        # A timeout is not a successful empty discovery and must remain retryable.
        route.mock(return_value=httpx.Response(200, json={"connectors": []}))
        if backend == "legacy":
            assert await registry.get_tools_async(force_refresh=True) == {}
            assert registry.bootstrap_error() is None
        else:
            catalog = await service.resolve_catalog(orchestrator, force_refresh=True)
            assert catalog is not None
            assert catalog.connectors == ()
        # respx only records responses; the cancelled request never produced one.
        assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["legacy", "catalog"])
async def test_cancelling_discovery_propagates_and_releases_lock(
    backend: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry, service, orchestrator = _clients(monkeypatch, tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        raise AssertionError("The stalled request must be cancelled")

    async def discover():
        if backend == "legacy":
            return await registry.get_tools_async(force_refresh=True)
        return await service.resolve_catalog(orchestrator, force_refresh=True)

    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=r".*/v1/connectors/bootstrap").mock(
            side_effect=stalled
        )
        task = asyncio.create_task(discover())
        try:
            await asyncio.wait_for(started.wait(), timeout=0.5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cancelled.is_set()
        assert registry.bootstrap_error() is None
        assert service.read_catalog(orchestrator).catalog is None
        route.mock(return_value=httpx.Response(200, json={"connectors": []}))
        await asyncio.wait_for(discover(), timeout=0.5)
        assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["legacy", "catalog"])
async def test_explicit_refresh_gets_longer_deadline_and_populates_cache(
    backend: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry, service, orchestrator = _clients(monkeypatch, tmp_path)
    payload = {
        "connectors": [
            {
                "id": "wiki",
                "name": "wiki",
                "status": {"is_ready": True},
                "tools": [{"name": "search", "inputSchema": {"type": "object"}}],
            }
        ]
    }

    async def slow(request: httpx.Request) -> httpx.Response:
        # Longer than automatic discovery's budget, within explicit refresh's.
        await asyncio.sleep(0.08)
        return httpx.Response(200, json=payload)

    with respx.mock as router:
        route = router.get(url__regex=r".*/v1/connectors/bootstrap").mock(
            side_effect=slow
        )
        if backend == "legacy":
            tools = await registry.get_tools_async(force_refresh=True)
            assert "connector_wiki_search" in tools
            assert await registry.get_tools_async() == tools
        else:
            catalog = await service.resolve_catalog(orchestrator, force_refresh=True)
            assert catalog is not None
            assert catalog.connectors[0].tools[0].raw_name == "search"
            assert await service.resolve_catalog(orchestrator) == catalog
        assert route.call_count == 1
