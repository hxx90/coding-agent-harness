from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.conftest import build_test_vibe_app, wait_until
from tests.stubs.fake_account_gateway import FakeAccountGateway
from tests.update_notifier.adapters.fake_update_cache_repository import (
    FakeUpdateCacheRepository,
)
from vibe.app_server._account import WhoAmIResult
from vibe.app_server.models import AccountPlanKind
from vibe.cli.textual_ui.widgets.messages import WhatsNewMessage
from vibe.cli.update_notifier import UpdateCache


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed", ["account", "identity", "both", "none"])
async def test_local_notes_show_before_network_and_keep_late_account_offer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, delayed: str
) -> None:
    (tmp_path / "whats_new.md").write_text(
        "# Local release notes\n\nOffline feature marker"
    )
    monkeypatch.setattr("vibe.cli.update_notifier.whats_new.VIBE_ROOT", tmp_path)
    cache = FakeUpdateCacheRepository(
        update_cache=UpdateCache(
            latest_version="1.0.0", stored_at_timestamp=0, seen_whats_new_version=None
        )
    )
    app = build_test_vibe_app(
        current_version="1.0.0",
        update_cache_repository=cache,
        account_gateway=FakeAccountGateway(
            WhoAmIResult(
                plan_type=AccountPlanKind.API,
                plan_name="FREE",
                prompt_switching_to_pro_plan=False,
            )
        ),
    )
    release = asyncio.Event()
    started = asyncio.Event()
    original_account = app._refresh_account
    original_identity = app._refresh_identity

    async def account():
        if delayed == "none":
            started.set()
        if delayed in {"account", "both"}:
            started.set()
            await release.wait()
        await original_account()

    async def identity():
        if delayed in {"identity", "both"}:
            started.set()
            await release.wait()
        await original_identity()

    monkeypatch.setattr(app, "_refresh_account", account)
    monkeypatch.setattr(app, "_refresh_identity", identity)
    async with app.run_test(size=(120, 36)) as pilot:
        try:
            assert await wait_until(pilot, started.is_set)
            assert await wait_until(
                pilot, lambda: bool(app.query(WhatsNewMessage)), timeout=0.5
            )
            note = app.query_one(WhatsNewMessage)
            assert not release.is_set()
            if delayed != "none":
                assert not app._startup_command_availability_ready.is_set()
            assert "Offline" in app.export_screenshot()
            app.save_screenshot("notes-before-network.svg", path=str(tmp_path))
            saved = await cache.get()
            assert saved is not None and saved.seen_whats_new_version == "1.0.0"
        finally:
            release.set()
        assert await wait_until(pilot, app._startup_command_availability_ready.is_set)
        assert await wait_until(pilot, lambda: "Upgrade" in app.export_screenshot())
        assert list(app.query(WhatsNewMessage)) == [note]
        app.save_screenshot("notes-after-network.svg", path=str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("close_app", [False, True])
async def test_pending_refresh_does_not_resurrect_notes_or_leak_on_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, close_app: bool
) -> None:
    (tmp_path / "whats_new.md").write_text("# Local notes")
    monkeypatch.setattr("vibe.cli.update_notifier.whats_new.VIBE_ROOT", tmp_path)
    cache = FakeUpdateCacheRepository(
        update_cache=UpdateCache(
            latest_version="1.0.0", stored_at_timestamp=0, seen_whats_new_version=None
        )
    )
    app = build_test_vibe_app(current_version="1.0.0", update_cache_repository=cache)
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stalled():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(app, "_refresh_account", stalled)
    async with app.run_test() as pilot:
        assert await wait_until(pilot, started.is_set)
        assert await wait_until(pilot, lambda: bool(app.query(WhatsNewMessage)))
        await app.query_one(WhatsNewMessage).remove()
        if not close_app:
            release.set()
            assert await wait_until(
                pilot, app._startup_command_availability_ready.is_set
            )
            await pilot.pause()
            assert not app.query(WhatsNewMessage)
    if close_app:
        assert cancelled.is_set()
