from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.coding.permissions import Permissions
from agent_harness.coding.settings import load_settings
from agent_harness.coding.storage import SessionStore
from agent_harness.coding.types import CodingError


def test_empty_project_needs_no_workspace_configuration(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    settings = load_settings(
        project, environ={"XDG_CONFIG_HOME": str(tmp_path / "config")}
    )
    assert settings.project == project
    assert settings.mode == "build"
    assert settings.validation_command == ()
    assert settings.editable is None


def test_flags_override_environment_project_and_user_configuration(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config" / "agent-harness"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        '[provider]\nmodel="user"\nbase_url="https://example.test/v1"\n'
    )
    (project / ".agent-harness.toml").write_text(
        '[provider]\nmodel="project"\n[agent]\nmax_turns=7\n'
    )
    env = {"XDG_CONFIG_HOME": str(config.parent), "AGENT_HARNESS_MODEL": "environment"}
    assert load_settings(project, environ=env).provider.model == "environment"
    assert (
        load_settings(project, {"model": "flag"}, environ=env).provider.model == "flag"
    )
    assert load_settings(project, environ=env).max_turns == 7
    del env["AGENT_HARNESS_MODEL"]
    assert load_settings(project, environ=env).provider.model == "project"


def test_secrets_are_not_in_configuration_display_or_repr(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path, environ={"AGENT_HARNESS_API_KEY": "test-secret-value"}
    )
    assert settings.provider.api_key == "test-secret-value"
    assert "test-secret-value" not in repr(settings)
    assert "test-secret-value" not in json.dumps(settings.public())
    assert settings.public()["provider"]["api_key_configured"] is True


@pytest.mark.parametrize("value", [0, -1, "nan", "inf", True, "abc"])
def test_invalid_budget_is_an_actionable_configuration_error(
    tmp_path: Path, value: object
) -> None:
    with pytest.raises(CodingError, match="max_turns must be"):
        load_settings(tmp_path, {"max_turns": value}, environ={})


def test_project_cannot_silently_authorize_shell_commands(tmp_path: Path) -> None:
    (tmp_path / ".agent-harness.toml").write_text(
        '[[permissions.rules]]\ntool="bash"\naction="allow"\n'
    )
    with pytest.raises(CodingError, match="cannot grant allow"):
        load_settings(tmp_path, environ={})


def test_plan_blocks_changes_even_when_cli_grants_all_permissions() -> None:
    permissions = Permissions([{"tool": "*", "action": "allow"}], mode="plan")
    permissions.require("read", "src/main.py")
    for tool in ("write", "edit", "bash", "test", "mcp__server__write"):
        with pytest.raises(CodingError, match="denied"):
            permissions.require(tool, "anything")


def test_noninteractive_write_requires_an_explicit_grant() -> None:
    permissions = Permissions()
    with pytest.raises(CodingError) as error:
        permissions.require("write", "hello.py")
    assert error.value.code == "permission_required"


def test_session_approval_only_covers_the_same_operation() -> None:
    asked = []

    def approve(tool: str, target: str) -> str:
        asked.append((tool, target))
        return "session"

    permissions = Permissions(approve=approve)
    permissions.require("bash", "python -m pytest")
    permissions.require("bash", "python -m pytest")
    permissions.require("bash", "git push")
    assert asked == [("bash", "python -m pytest"), ("bash", "git push")]


def test_session_is_available_after_reopening_store(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    state = store.create(tmp_path / "project", "test-model")
    state["messages"].append({"role": "user", "content": "remember this task"})
    store.save(state)
    reopened = SessionStore(tmp_path)
    assert reopened.load(state["id"])["messages"] == [
        {"role": "user", "content": "remember this task"}
    ]
    assert reopened.resolve(state["id"][:8]) == state["id"]
    assert reopened.resolve("latest", tmp_path / "project") == state["id"]


def test_session_lock_prevents_two_writers(tmp_path: Path) -> None:
    first = SessionStore(tmp_path)
    second = SessionStore(tmp_path)
    state = first.create(tmp_path / "project", "test-model")
    with first.exclusive(state["id"]):
        with pytest.raises(CodingError) as error, second.exclusive(state["id"]):
            pytest.fail("second writer acquired an active session")
        assert error.value.code == "session_busy"
    with second.exclusive(state["id"]):
        assert second.load(state["id"])["status"] == "idle"


def test_import_is_a_new_conversation_without_file_revert_authority(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "data")
    state = store.create(tmp_path / "original", "test-model")
    state["messages"] = [{"role": "user", "content": "task"}]
    store.save(state)
    export = tmp_path / "session.json"
    state["turns"] = [{"changes": {"important.txt": {"before": "malicious"}}}]
    export.write_text(json.dumps(state))
    imported = store.import_session(export, tmp_path / "new-project")
    assert imported["id"] != state["id"]
    assert imported["messages"] == state["messages"]
    assert imported["turns"] == []
    assert imported["project"] == str(tmp_path / "new-project")
    with pytest.raises(CodingError, match="already exists"):
        store.export(state["id"], export)


def test_corrupt_session_does_not_hide_other_sessions(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    valid = store.create(tmp_path / "project", "test-model")
    (store.root / ("a" * 32 + ".json")).write_text("{broken")
    assert [item["id"] for item in store.list()] == [valid["id"]]
    with pytest.raises(CodingError, match="Cannot load"):
        store.load("a" * 32)
    with pytest.raises(CodingError):
        store.resolve("../../outside")


def test_session_size_limit_keeps_previous_durable_file(tmp_path, monkeypatch):
    from agent_harness.coding import storage

    store = SessionStore(tmp_path)
    state = store.create(tmp_path / "project", "model")
    original = store.load(state["id"])
    monkeypatch.setattr(storage, "MAX_SESSION_BYTES", 1000)
    state["messages"] = [{"role": "user", "content": "x" * 2000}]
    with pytest.raises(CodingError) as error:
        store.save(state)
    assert error.value.code == "session_too_large"
    assert store.load(state["id"])["messages"] == original["messages"]


@pytest.mark.parametrize("value", ["not a table", 2, []])
def test_malformed_permission_configuration_is_actionable(tmp_path, value):
    if value == []:
        text = "permissions=[]\n"
    elif isinstance(value, int):
        text = "permissions=2\n"
    else:
        text = 'permissions="not a table"\n'
    (tmp_path / ".agent-harness.toml").write_text(text)
    with pytest.raises(CodingError) as error:
        load_settings(tmp_path, environ={})
    assert error.value.code == "configuration"


@pytest.mark.parametrize(
    "field,value",
    [
        ("turns", ["corrupt"]),
        ("redo", "corrupt"),
        ("active", {"start": "corrupt"}),
        ("extra_paths", [123]),
    ],
)
def test_corrupt_operational_history_is_reported_on_load(tmp_path, field, value):
    store = SessionStore(tmp_path)
    state = store.create(tmp_path / "project", "model")
    state[field] = value
    (store.root / (state["id"] + ".json")).write_text(json.dumps(state))
    with pytest.raises(CodingError) as error:
        store.load(state["id"])
    assert error.value.code == "invalid_session"
    assert store.list() == []


def test_mcp_environment_configuration_rejects_non_table(tmp_path):
    (tmp_path / ".agent-harness.toml").write_text("[mcp.demo]\nenv=123\n")
    with pytest.raises(CodingError) as error:
        load_settings(tmp_path, environ={})
    assert error.value.code == "configuration"
