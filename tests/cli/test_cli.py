from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import Reply, answer, call

CLI = str(Path(sys.executable).parent / "agent-harness")


def execute(cli_env, *arguments, stdin=""):
    project, env = cli_env
    return subprocess.run(
        [CLI, "-C", str(project), *arguments],
        env=env,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_installed_help_version_config_and_missing_configuration(cli_env):
    assert execute(cli_env, "--version").stdout.strip() == "2.0.0"
    assert "sessions" in execute(cli_env, "--help").stdout
    assert json.loads(execute(cli_env, "config", "show").stdout)["mode"] == "build"
    result = execute(cli_env, "run", "Explain", "--json")
    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(e.get("code") == "configuration" for e in events)
    assert "Traceback" not in result.stderr


def test_json_stdin_task_resume_sessions_export_import_and_undo(
    cli_env, model_server, tmp_path
):
    url, requests, _ = model_server(
        [
            answer("", [call("write", {"path": "hello.txt", "content": "hello\n"})]),
            answer("Created hello"),
            answer("It says hello"),
        ]
    )
    cli_env[1].update(AGENT_HARNESS_BASE_URL=url, AGENT_HARNESS_MODEL="test-model")
    result = execute(
        cli_env, "run", "--json", "--allow", "write", stdin="Create a hello file"
    )
    assert result.returncode == 0, result.stderr + result.stdout
    events = [json.loads(line) for line in result.stdout.splitlines()]
    final = events[-1]
    assert final["type"] == "result" and final["status"] == "completed"
    identifier = final["session_id"]
    assert (cli_env[0] / "hello.txt").read_text() == "hello\n"
    result = execute(
        cli_env, "run", "What does it say?", "--session", identifier, "--json"
    )
    assert result.returncode == 0
    assert len(requests) == 3 and "Create a hello file" in str(requests[-1])
    assert (
        json.loads(execute(cli_env, "sessions", "list").stdout)[0]["id"] == identifier
    )
    assert "Created hello" in execute(cli_env, "sessions", "show", identifier).stdout
    exported = tmp_path / "export.json"
    assert (
        execute(cli_env, "sessions", "export", identifier, str(exported)).returncode
        == 0
    )
    imported = execute(cli_env, "sessions", "import", str(exported))
    assert json.loads(imported.stdout)["session_id"] != identifier
    assert execute(cli_env, "undo", identifier).returncode == 0  # latest question only
    assert "+hello" in execute(cli_env, "diff", identifier).stdout
    assert execute(cli_env, "undo", identifier).returncode == 0
    assert not (cli_env[0] / "hello.txt").exists()
    assert execute(cli_env, "redo", identifier).returncode == 0
    assert (cli_env[0] / "hello.txt").read_text() == "hello\n"


def test_model_listing_doctor_and_private_auth_storage(cli_env, model_server):
    url, _, _ = model_server([])
    cli_env[1].update(AGENT_HARNESS_BASE_URL=url, AGENT_HARNESS_MODEL="test-model")
    assert json.loads(execute(cli_env, "models").stdout) == ["test-model"]
    assert json.loads(execute(cli_env, "doctor").stdout)["connection"] == "ok"
    secret = "fixture-secret-key"
    result = execute(cli_env, "auth", "login", "--stdin", stdin=secret + "\n")
    assert result.returncode == 0 and secret not in result.stdout + result.stderr
    path = Path(cli_env[1]["XDG_CONFIG_HOME"]) / "agent-harness" / "credentials.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert secret not in execute(cli_env, "config", "show").stdout
    assert (
        json.loads(execute(cli_env, "auth", "status").stdout)["key_configured"] is True
    )
    assert execute(cli_env, "auth", "logout").returncode == 0
    assert (
        json.loads(execute(cli_env, "auth", "status").stdout)["key_configured"] is False
    )


def test_exit_codes_for_permissions_budget_and_bad_arguments(cli_env, model_server):
    url, _, _ = model_server(
        [
            answer("", [call("write", {"path": "x", "content": "x"})]),
            answer("", [call("glob", {})]),
        ]
    )
    cli_env[1].update(AGENT_HARNESS_BASE_URL=url, AGENT_HARNESS_MODEL="test-model")
    assert execute(cli_env, "run", "Write").returncode == 3
    assert execute(cli_env, "run", "Explore", "--max-turns", "1").returncode == 4
    assert execute(cli_env, "run", "--unknown").returncode == 2


class Terminal:
    def __init__(self, cli_env):
        import pty

        self.master, slave = pty.openpty()
        project, env = cli_env
        self.process = subprocess.Popen(
            [CLI, "-C", str(project)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
        self.buffer = ""
        self.transcript = ""

    def send(self, text):
        os.write(self.master, text.encode())

    def expect(self, text, timeout=8):
        deadline = time.monotonic() + timeout
        while text not in self.buffer:
            if time.monotonic() >= deadline:
                raise AssertionError(f"Missing {text!r}: {self.transcript}")
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    piece = os.read(self.master, 65536).decode(errors="replace")
                except OSError:
                    piece = ""
                if not piece:
                    raise AssertionError(
                        f"Terminal exited before {text!r}: {self.transcript}"
                    )
                self.buffer += piece
                self.transcript += piece
        self.buffer = self.buffer.split(text, 1)[1]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(2)
        os.close(self.master)


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY")
def test_real_terminal_multiturn_permission_slash_commands_and_exit(
    cli_env, model_server
):
    url, requests, _ = model_server(
        [
            answer("", [call("write", {"path": "pty.txt", "content": "created"})]),
            answer("PTY task completed"),
            answer("Second answer"),
        ]
    )
    cli_env[1].update(AGENT_HARNESS_BASE_URL=url, AGENT_HARNESS_MODEL="test-model")
    terminal = Terminal(cli_env)
    try:
        terminal.expect("build>")
        terminal.send("Create file\n")
        terminal.expect("Allow [y]")
        terminal.send("y\n")
        terminal.expect("PTY task completed")
        terminal.expect("build>")
        assert (cli_env[0] / "pty.txt").read_text() == "created"
        terminal.send("Tell me about it\n")
        terminal.expect("Second answer")
        terminal.expect("build>")
        terminal.send("/mode plan\n")
        terminal.expect("plan>")
        terminal.send("/status\n")
        terminal.expect('"mode": "plan"')
        terminal.expect("plan>")
        terminal.send("/exit\n")
        terminal.process.wait(3)
        assert terminal.process.returncode == 0
        assert len(requests) == 3
    finally:
        terminal.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY")
def test_terminal_ctrl_c_cancels_http_and_allows_next_task(cli_env, model_server):
    url, requests, _ = model_server(
        [Reply(body=answer("late"), delay_headers=5), answer("After cancellation")]
    )
    cli_env[1].update(AGENT_HARNESS_BASE_URL=url, AGENT_HARNESS_MODEL="test-model")
    terminal = Terminal(cli_env)
    try:
        terminal.expect("build>")
        terminal.send("Slow request\n")
        deadline = time.monotonic() + 3
        while not requests and time.monotonic() < deadline:
            time.sleep(0.02)
        assert requests
        os.kill(terminal.process.pid, signal.SIGINT)
        terminal.expect("[cancelled]")
        terminal.expect("build>")
        terminal.send("Continue\n")
        terminal.expect("After cancellation")
        terminal.expect("build>")
        terminal.send("/exit\n")
        terminal.process.wait(3)
        assert terminal.process.returncode == 0
    finally:
        terminal.close()


def test_session_lock_is_enforced_across_processes(cli_env, model_server):
    from agent_harness.coding.storage import SessionStore

    store = SessionStore(Path(cli_env[1]["AGENT_HARNESS_DATA_DIR"]))
    state = store.create(cli_env[0], "test-model")
    with store.exclusive(state["id"]):
        result = execute(cli_env, "run", "Continue", "--session", state["id"], "--json")
    assert result.returncode == 1 and "session_busy" in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY")
def test_terminal_ctrl_c_reaps_shell_and_keeps_changes(cli_env, model_server):
    import shlex

    source = "import time; open('started.txt','w').write('kept'); time.sleep(20)"
    command = shlex.join([sys.executable, "-c", source])
    url, _, _ = model_server(
        [
            answer(
                "",
                [
                    call(
                        "bash", {"command": command, "description": "Cancellation test"}
                    )
                ],
            )
        ]
    )
    cli_env[1].update(AGENT_HARNESS_BASE_URL=url, AGENT_HARNESS_MODEL="test-model")
    terminal = Terminal(cli_env)
    try:
        terminal.expect("build>")
        terminal.send("Run command\n")
        terminal.expect("Allow [y]")
        terminal.send("y\n")
        deadline = time.monotonic() + 3
        while not (cli_env[0] / "started.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (cli_env[0] / "started.txt").read_text() == "kept"
        os.kill(terminal.process.pid, signal.SIGINT)
        terminal.expect("[cancelled]")
        terminal.expect("build>")
        terminal.send("/exit\n")
        terminal.process.wait(3)
        assert terminal.process.returncode == 0
    finally:
        terminal.close()
