from __future__ import annotations

import json
import os
import signal
import sys
import threading

import pytest
from conftest import answer, call
from test_cli import Terminal

from agent_harness.coding.permissions import Permissions
from agent_harness.coding.session import CodingSession
from agent_harness.coding.settings import ProviderSettings, Settings
from agent_harness.coding.tools import ToolSet
from agent_harness.coding.types import CodingError
from agent_harness.coding.workspace import Workspace, command_environment, run_command


def session_at(tmp_path, model_server, responses, **kwargs):
    project = tmp_path / "project"
    project.mkdir()
    url, requests, _ = model_server(responses)
    settings = Settings(
        project,
        tmp_path / "data",
        ProviderSettings(base_url=url, model="test-model"),
        **kwargs,
    )
    return CodingSession(settings), requests


@pytest.mark.parametrize("name", ["read", "write", "edit"])
@pytest.mark.parametrize("alias", ["locked.txt", "./locked.txt", "absolute"])
def test_file_permission_rules_use_canonical_path(tmp_path, name, alias):
    settings = Settings(tmp_path, tmp_path.parent / "data", ProviderSettings())
    workspace = Workspace(settings)
    (tmp_path / "locked.txt").write_text("original")
    workspace.read("locked.txt")
    permissions = Permissions(
        [
            {"tool": "*", "action": "allow"},
            {"tool": name, "pattern": "locked.txt", "action": "deny"},
        ]
    )
    tools = ToolSet(settings, workspace, permissions, {"todos": []}, threading.Event())
    arguments = {"path": str(tmp_path / "locked.txt") if alias == "absolute" else alias}
    if name == "write":
        arguments["content"] = "replacement"
    elif name == "edit":
        arguments.update(old_string="original", new_string="replacement")
    with pytest.raises(CodingError) as exc:
        tools.execute(name, json.dumps(arguments))
    assert exc.value.code == "permission_denied"
    assert (tmp_path / "locked.txt").read_text() == "original"


def test_cancel_in_approval_callback_never_executes_write(tmp_path):
    settings = Settings(tmp_path, tmp_path.parent / "data", ProviderSettings())
    stop = threading.Event()

    def approve(tool, target):
        stop.set()
        return "once"

    tools = ToolSet(
        settings, Workspace(settings), Permissions(approve=approve), {"todos": []}, stop
    )
    with pytest.raises(CodingError) as exc:
        tools.execute(
            "write", json.dumps({"path": "after-cancel.txt", "content": "bad"})
        )
    assert exc.value.code == "cancelled"
    assert not (tmp_path / "after-cancel.txt").exists()


def test_external_edit_during_model_wait_is_not_part_of_undo(tmp_path, model_server):
    def respond(request, index):
        (tmp_path / "project" / "user.txt").write_text("user's new work")
        return answer("A pure answer")

    session, _ = session_at(tmp_path, model_server, respond)
    path = session.settings.project / "user.txt"
    path.write_text("original")
    assert session.run("Explain only").status == "completed"
    assert session.diff() == ""
    session.undo()
    assert path.read_text() == "user's new work"


def test_tool_undo_preserves_unrelated_external_edit_during_model_wait(
    tmp_path, model_server
):
    def respond(request, index):
        if index == 0:
            return answer(
                "", [call("write", {"path": "agent.txt", "content": "created"})]
            )
        (tmp_path / "project" / "user.txt").write_text("user's new work")
        return answer("Done")

    session, _ = session_at(
        tmp_path,
        model_server,
        respond,
        permission_rules=({"tool": "*", "action": "allow"},),
    )
    path = session.settings.project / "user.txt"
    path.write_text("original")
    assert session.run("Create file").status == "completed"
    session.undo()
    assert path.read_text() == "user's new work"
    assert not (session.settings.project / "agent.txt").exists()


def test_short_mcp_values_do_not_corrupt_ids_or_snapshots(tmp_path, model_server):
    session, _ = session_at(
        tmp_path,
        model_server,
        [
            answer("", [call("write", {"path": "file.txt", "content": "1"})]),
            answer("ready"),
        ],
        permission_rules=({"tool": "*", "action": "allow"},),
        mcp={"inactive": {"enabled": False, "env": {"DEBUG": "1", "TOKEN": "1"}}},
    )
    identifier = session.id
    assert session.run("Write file").status == "completed"
    assert session.store.load(identifier)["id"] == identifier
    assert (session.settings.project / "file.txt").read_text() == "1"
    session.undo()
    assert not (session.settings.project / "file.txt").exists()
    session.undo(redo=True)
    assert (session.settings.project / "file.txt").read_text() == "1"


def test_closed_output_does_not_shorten_command_timeout(tmp_path):
    result = run_command(
        [
            sys.executable,
            "-c",
            "import os,time; os.close(1); os.close(2); time.sleep(2.3)",
        ],
        tmp_path,
        timeout=4,
        stop=threading.Event(),
    )
    assert result["exit_code"] == 0 and not result["timed_out"]
    assert result["duration_seconds"] >= 2.3


def test_command_environment_scrubs_api_key_spellings_and_values(monkeypatch):
    monkeypatch.setenv("REVIEW_APIKEY", "private-value")
    monkeypatch.setenv("CUSTOM_NAME", "configured-provider-secret")
    environment = command_environment(("configured-provider-secret",))
    assert "REVIEW_APIKEY" not in environment
    assert "CUSTOM_NAME" not in environment


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY")
def test_ctrl_c_cancels_permission_prompt_without_an_answer(cli_env, model_server):
    url, _, _ = model_server(
        [
            answer("", [call("write", {"path": "bad.txt", "content": "bad"})]),
            answer("Recovered"),
        ]
    )
    cli_env[1].update(AGENT_HARNESS_BASE_URL=url, AGENT_HARNESS_MODEL="test-model")
    terminal = Terminal(cli_env)
    try:
        terminal.expect("build>")
        terminal.send("Write file\n")
        terminal.expect("Allow [y]")
        os.kill(terminal.process.pid, signal.SIGINT)
        terminal.expect("[cancelled]", timeout=2)
        terminal.expect("build>")
        assert not (cli_env[0] / "bad.txt").exists()
        terminal.send("Continue\n")
        terminal.expect("Recovered")
        terminal.expect("build>")
        terminal.send("/exit\n")
        terminal.process.wait(3)
        assert terminal.process.returncode == 0
    finally:
        terminal.close()
