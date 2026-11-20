"""Discovery shared by config reload workers must work across event loops."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import threading
from unittest.mock import AsyncMock

import pytest

from vibe.core.tools.connectors.connector_registry import ConnectorRegistry


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> ConnectorRegistry:
    registry = ConnectorRegistry(api_key="test-key")
    monkeypatch.setattr(registry, "_read_cached_bootstrap", lambda: None)
    monkeypatch.setattr(registry, "_write_cached_bootstrap", lambda payload: None)
    return registry


def test_discovery_wakes_waiter_on_another_loop(
    registry: ConnectorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    waiter_loop: list[asyncio.AbstractEventLoop] = []
    waiter_tasks: list[asyncio.Task] = []

    async def fetch():
        started.set()
        assert await asyncio.to_thread(release.wait, 3)
        return {"connectors": []}

    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(registry, "_fetch_bootstrap", fetch_mock)

    async def wait_for_discovery():
        waiter_loop.append(asyncio.get_running_loop())
        task = asyncio.create_task(registry.get_tools_async())
        waiter_tasks.append(task)
        await asyncio.sleep(0)
        waiting.set()
        return await task

    completed_without_wakeup = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(asyncio.run, registry.get_tools_async())
        try:
            assert started.wait(3)
            waiter = pool.submit(asyncio.run, wait_for_discovery())
            assert waiting.wait(3)
            release.set()
            assert owner.result(timeout=3) == {}
            try:
                assert waiter.result(timeout=1) == {}
                completed_without_wakeup = True
            except FutureTimeoutError:
                # Bounded cleanup of the unfixed lock: never count this as success.
                waiter_loop[0].call_soon_threadsafe(waiter_tasks[0].cancel)
        finally:
            release.set()
    assert completed_without_wakeup, "discovery did not wake the other event loop"
    assert fetch_mock.await_count == 1
    # Reuse after both loops have closed; explicit refresh must still fetch.
    assert asyncio.run(registry.get_tools_async()) == {}
    assert asyncio.run(registry.get_tools_async(force_refresh=True)) == {}
    assert fetch_mock.await_count == 2


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_discovery(
    registry: ConnectorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch():
        started.set()
        await release.wait()
        return {"connectors": []}

    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(registry, "_fetch_bootstrap", fetch_mock)
    owner = asyncio.create_task(registry.get_tools_async())
    await started.wait()
    cancelled_waiter = asyncio.create_task(registry.get_tools_async())
    surviving_waiter = asyncio.create_task(registry.get_tools_async())
    await asyncio.sleep(0)
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    release.set()
    assert await asyncio.wait_for(asyncio.gather(owner, surviving_waiter), 1) == [
        {},
        {},
    ]
    assert fetch_mock.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_owner", [True, False])
async def test_owner_cancellation_or_failure_releases_waiter_for_retry(
    registry: ConnectorRegistry, monkeypatch: pytest.MonkeyPatch, cancel_owner: bool
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def fetch():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await release.wait()
            raise RuntimeError("bootstrap failed")
        return {"connectors": []}

    monkeypatch.setattr(registry, "_fetch_bootstrap", fetch)
    owner = asyncio.create_task(registry.get_tools_async())
    await started.wait()
    waiter = asyncio.create_task(registry.get_tools_async())
    await asyncio.sleep(0)
    if cancel_owner:
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    else:
        release.set()
        assert await owner == {}
    assert await asyncio.wait_for(waiter, 1) == {}
    assert await registry.get_tools_async() == {}
    assert attempts == 2


@pytest.mark.asyncio
async def test_concurrent_explicit_refreshes_remain_serialized(
    registry: ConnectorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = 0
    peak = 0
    calls = 0

    async def fetch():
        nonlocal active, peak, calls
        active += 1
        peak = max(peak, active)
        calls += 1
        try:
            await asyncio.sleep(0)
            return {"connectors": []}
        finally:
            active -= 1

    monkeypatch.setattr(registry, "_fetch_bootstrap", fetch)
    assert await asyncio.wait_for(
        asyncio.gather(
            *(registry.get_tools_async(force_refresh=True) for _ in range(3))
        ),
        1,
    ) == [{}, {}, {}]
    assert calls == 3
    assert peak == 1
