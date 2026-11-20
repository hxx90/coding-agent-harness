from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import pytest_asyncio
from textual.worker import WorkerCancelled

from tests.conftest import build_test_vibe_app
from vibe.app_server import AppServerSession
from vibe.cli.textual_ui.app import VibeApp, run_textual_ui
from vibe.cli.textual_ui.widgets.session_picker import SessionPickerApp
from vibe.config_values import AUTO_THEME


@pytest_asyncio.fixture
async def prepared_app():
    before = asyncio.all_tasks()
    apps: list[VibeApp] = []

    async def create(*, initial_prompt: str | None = None) -> VibeApp:
        app = build_test_vibe_app(initial_prompt=initial_prompt)
        apps.append(app)
        await app.prepare()
        return app

    try:
        yield create
    finally:
        # prepare() starts server/background tasks even without mounting the TUI.
        # Match the production entry point instead of leaving pytest to cancel
        # parent and low-level connection tasks together at loop shutdown.
        for app in reversed(apps):
            workers = list(app.workers)
            app.workers.cancel_all()
            try:
                for worker in workers:
                    with suppress(WorkerCancelled):
                        await worker.wait()
            finally:
                await app.shutdown_cleanup()
    await asyncio.sleep(0)
    leaked = [
        task
        for task in asyncio.all_tasks() - before
        if task is not asyncio.current_task() and not task.done()
    ]
    assert not leaked, [(task.get_name(), str(task.get_coro())) for task in leaked]


@pytest.mark.asyncio
async def test_startup_prompt_waits_for_startup_resume_picker(
    monkeypatch: pytest.MonkeyPatch, prepared_app
) -> None:
    app = await prepared_app(initial_prompt="continue the work")
    app._show_resume_picker = True
    process_prompt = Mock()

    monkeypatch.setattr(app, "_refresh_account", AsyncMock())
    monkeypatch.setattr(app, "_check_and_show_whats_new", AsyncMock())
    monkeypatch.setattr(app, "_show_greeting_message", AsyncMock())
    monkeypatch.setattr(app, "_schedule_update_notification", Mock())
    monkeypatch.setattr(app, "_process_initial_prompt", process_prompt)

    await app._complete_post_ready_startup()

    process_prompt.assert_not_called()


@pytest.mark.asyncio
async def test_startup_prompt_runs_after_startup_resume_picker_selection(
    monkeypatch: pytest.MonkeyPatch, prepared_app
) -> None:
    app = await prepared_app(initial_prompt="continue the work")
    app._show_resume_picker = True
    app._startup_command_availability_ready.set()
    process_prompt = Mock()

    monkeypatch.setattr(app, "_switch_to_input_app", AsyncMock())
    monkeypatch.setattr(app, "_resume_local_session", AsyncMock())
    monkeypatch.setattr(app, "_process_initial_prompt", process_prompt)

    await app.on_session_picker_app_session_selected(
        SessionPickerApp.SessionSelected("local:session-1", "session-1")
    )

    assert app._show_resume_picker is False
    process_prompt.assert_called_once_with()


@pytest.mark.asyncio
async def test_startup_teleport_waits_for_account_read_after_session_selection(
    monkeypatch: pytest.MonkeyPatch, prepared_app
) -> None:
    app = await prepared_app(initial_prompt="continue the work")
    app._show_resume_picker = True
    app._teleport_on_start = True
    run_worker = Mock()
    handle_teleport = Mock(return_value=object())
    handle_user_message = Mock(return_value=object())

    monkeypatch.setattr(app, "_switch_to_input_app", AsyncMock())
    monkeypatch.setattr(app, "_resume_local_session", AsyncMock())
    monkeypatch.setattr(app, "run_worker", run_worker)
    monkeypatch.setattr(app, "_handle_teleport_command", handle_teleport)
    monkeypatch.setattr(app, "_handle_user_message", handle_user_message)
    monkeypatch.setattr(app.commands, "has_command", lambda name: name == "teleport")

    task = asyncio.create_task(
        app.on_session_picker_app_session_selected(
            SessionPickerApp.SessionSelected("local:session-1", "session-1")
        )
    )
    await asyncio.sleep(0)

    handle_teleport.assert_not_called()
    handle_user_message.assert_not_called()

    app._refresh_command_registry()
    app._startup_command_availability_ready.set()
    await task

    handle_teleport.assert_called_once_with("continue the work")
    handle_user_message.assert_not_called()
    run_worker.assert_called_once_with(handle_teleport.return_value, exclusive=False)


@pytest.mark.asyncio
async def test_failed_resume_restores_transcript_when_previewing(
    monkeypatch: pytest.MonkeyPatch, prepared_app
) -> None:
    app = await prepared_app()
    app._picker.previewing = True
    app._cached_messages_area = MagicMock(remove_children=AsyncMock())

    reset_ui = Mock()
    resume_history = AsyncMock()
    mount = AsyncMock()
    monkeypatch.setattr(app, "_switch_to_input_app", AsyncMock())
    monkeypatch.setattr(
        app, "_resume_local_session", AsyncMock(side_effect=RuntimeError("boom"))
    )
    monkeypatch.setattr(app, "_reset_ui_state", reset_ui)
    monkeypatch.setattr(app, "_resume_history_from_messages", resume_history)
    monkeypatch.setattr(app, "_mount_and_scroll", mount)
    monkeypatch.setattr(app, "_load_more", MagicMock(hide=AsyncMock()))

    await app.on_session_picker_app_session_selected(
        SessionPickerApp.SessionSelected("local:session-1", "session-1")
    )

    # The previewed transcript is rebuilt from the still-current session so the
    # failed session's preview is not left on screen desynced from session_id.
    reset_ui.assert_called_once_with()
    resume_history.assert_awaited_once_with()
    assert mount.await_count >= 1


@pytest.mark.asyncio
async def test_failed_resume_skips_restore_when_not_previewing(
    monkeypatch: pytest.MonkeyPatch, prepared_app
) -> None:
    app = await prepared_app()
    app._picker.previewing = False

    reset_ui = Mock()
    resume_history = AsyncMock()
    monkeypatch.setattr(app, "_switch_to_input_app", AsyncMock())
    monkeypatch.setattr(
        app, "_resume_local_session", AsyncMock(side_effect=RuntimeError("boom"))
    )
    monkeypatch.setattr(app, "_reset_ui_state", reset_ui)
    monkeypatch.setattr(app, "_resume_history_from_messages", resume_history)
    monkeypatch.setattr(app, "_mount_and_scroll", AsyncMock())

    await app.on_session_picker_app_session_selected(
        SessionPickerApp.SessionSelected("local:session-1", "session-1")
    )

    # No preview was shown, so there is nothing to rebuild.
    reset_ui.assert_not_called()
    resume_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_picker_to_input_clears_preview_and_startup_state(
    monkeypatch: pytest.MonkeyPatch, prepared_app
) -> None:
    app = await prepared_app()
    app._show_resume_picker = True
    app._startup_prompt_processed = False
    app._picker.previewing = True
    app._picker.preview_session_id = "session-1"

    rebuild = AsyncMock()
    monkeypatch.setattr(app, "_switch_to_input_app", AsyncMock())
    monkeypatch.setattr(app, "_rebuild_transcript_from_current_session", rebuild)

    await app._exit_picker_to_input()

    assert app._picker.preview_session_id is None
    assert app._show_resume_picker is False
    assert app._startup_prompt_processed is True
    assert app._picker.previewing is False
    rebuild.assert_awaited_once_with()


@pytest.mark.parametrize("theme", [AUTO_THEME, "dracula"])
def test_run_textual_ui_warms_auto_theme_before_app_server_start(
    theme: str, tmp_path
) -> None:
    app_server = object.__new__(AppServerSession)
    app_server.resources = MagicMock()
    app_server.resources.config.current.theme = theme
    start_app_server = AsyncMock(return_value=app_server)
    promo_repository = MagicMock()
    promo_repository.get = AsyncMock(return_value=None)

    with (
        patch("vibe.cli.textual_ui.app.resolve_auto_theme") as resolve_auto_theme,
        patch("vibe.cli.textual_ui.app.VibeApp") as vibe_app,
        patch(
            "vibe.cli.textual_ui.app.FileSystemVscodeExtensionPromoRepository",
            return_value=promo_repository,
        ),
        patch(
            "vibe.cli.textual_ui.app._run_app_with_cleanup",
            new=AsyncMock(return_value=None),
        ),
    ):

        async def start_app_server_after_theme_detection() -> AppServerSession:
            resolve_auto_theme.assert_called_once_with()
            return app_server

        start_app_server.side_effect = start_app_server_after_theme_detection
        run_textual_ui(
            start_app_server=start_app_server,
            history_file=tmp_path / "history",
            update_cache_repository=MagicMock(),
        )

    resolve_auto_theme.assert_called_once_with()
    vibe_app.assert_called_once()
