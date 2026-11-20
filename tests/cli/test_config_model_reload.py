"""User journey regression for overlapping config/model runtime reloads."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tomllib
from unittest.mock import AsyncMock

import pytest
from textual.widgets import OptionList
import tomli_w

from tests.cli.test_ui_config_screen import _filter_to, _open_editor
from tests.conftest import build_test_vibe_app, wait_until
from tests.stubs.fake_backend import FakeBackend
from tests.stubs.fake_mcp_registry import FakeMCPRegistry
from vibe.cli.textual_ui.screens.config import ConfigScreen
from vibe.cli.textual_ui.widgets.chat_input.container import ChatInputContainer
from vibe.cli.textual_ui.widgets.model_picker import ModelPickerApp
from vibe.core.agent_loop import AgentLoop
from vibe.core.config import build_default_orchestrator
from vibe.core.config.admin_config import ManagedConfigResult
from vibe.core.tools.connectors.connector_registry import ConnectorRegistry


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_save_theme_then_switch_model(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep discovery retryable during successive reloads without real network.
    async def unavailable_bootstrap(self):
        await asyncio.sleep(0.2)
        raise RuntimeError("test connector service unavailable")

    monkeypatch.setattr(
        "vibe.app_server._admin_config.fetch_managed_config",
        AsyncMock(return_value=ManagedConfigResult()),
    )
    monkeypatch.setattr(
        "vibe.app_server.connector_catalog._fetch_bootstrap",
        AsyncMock(side_effect=RuntimeError("test connector service unavailable")),
    )
    monkeypatch.setenv("VIBE_HOME", str(config_dir))
    monkeypatch.setattr(ConnectorRegistry, "_fetch_bootstrap", unavailable_bootstrap)
    monkeypatch.setattr(ConnectorRegistry, "_read_cached_bootstrap", lambda self: None)
    config_file = config_dir / "config.toml"
    data = tomllib.loads(config_file.read_text())
    data.update(
        models=[
            {"name": "qa-a", "provider": "mistral", "alias": "qa-a"},
            {"name": "qa-b", "provider": "mistral", "alias": "qa-b"},
        ],
        active_model="qa-a",
        theme="textual-dark",
        enable_connectors=True,
    )
    config_file.write_text(tomli_w.dumps(data))
    orchestrator = await build_default_orchestrator()
    agent = AgentLoop(
        config_orchestrator=orchestrator,
        backend=FakeBackend(),
        mcp_registry=FakeMCPRegistry(),
    )
    app = build_test_vibe_app(agent_loop=agent)
    async with app.run_test(size=(80, 24)) as pilot:
        await app._ensure_runtime_ready()
        app.query_one(ChatInputContainer).post_message(
            ChatInputContainer.Submitted("/config")
        )
        assert await wait_until(
            pilot,
            lambda: isinstance(app.screen, ConfigScreen) and bool(app.screen._views),
        )
        screen = app.screen
        assert isinstance(screen, ConfigScreen)
        await _filter_to(pilot, screen, "theme", "theme")
        await _open_editor(app, pilot)
        await pilot.press("down", "enter")
        assert await wait_until(
            pilot, lambda: orchestrator.config.theme != "textual-dark"
        )
        theme = orchestrator.config.theme
        assert tomllib.loads(config_file.read_text())["theme"] == theme
        await pilot.press("escape")
        app.query_one(ChatInputContainer).post_message(
            ChatInputContainer.Submitted("/model")
        )
        assert await wait_until(pilot, lambda: bool(app.query(ModelPickerApp)))
        options = app.query_one(ModelPickerApp).query_one(OptionList)
        assert await wait_until(
            pilot, lambda: options.option_count > 1 and options.has_focus
        )
        # Read the row selected by the keyboard: config layers merge models.
        await pilot.press("down")
        assert options.highlighted is not None
        selected_alias = options.get_option_at_index(options.highlighted).id
        assert selected_alias and selected_alias != "\x00default"
        assert selected_alias != app.config.active_model.alias
        await pilot.press("enter")
        assert await wait_until(
            pilot,
            lambda: (
                app.config.active_model.alias == selected_alias
                and not app.query(ModelPickerApp)
            ),
            timeout=5,
        ), {
            "selected": selected_alias,
            "ui": app.config.active_model.alias,
            "agent": agent.config.active_model,
            "picker_open": bool(app.query(ModelPickerApp)),
            "stored": tomllib.loads(config_file.read_text()).get("active_model"),
        }
        assert agent.config.active_model == selected_alias
        assert app.config.theme == theme
        assert tomllib.loads(config_file.read_text())["active_model"] == selected_alias
        app.save_screenshot("config-model-reload.svg", path=str(tmp_path))
        # Input remains usable after the full runtime reload, without a model call.
        app.query_one(ChatInputContainer).value = "ready for next task"
        assert app.query_one(ChatInputContainer).value == "ready for next task"
