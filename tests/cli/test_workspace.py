from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_harness.coding.settings import load_settings
from agent_harness.coding.types import CodingError
from agent_harness.coding.workspace import Workspace, run_command


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    project = tmp_path / "project"
    project.mkdir()
    return Workspace(load_settings(project, environ={}))


def test_read_edit_diff_undo_and_redo_use_real_files(workspace: Workspace) -> None:
    source = workspace.root / "main.py"
    source.write_text("value = 1\n")
    source.chmod(0o755)
    before = workspace.capture()
    assert "1: value = 1" in workspace.read("main.py")["content"]
    workspace.edit("main.py", "value = 1", "value = 2")
    workspace.write("new.txt", "created\n")
    changes = workspace.changes(before)
    assert set(changes) == {"main.py", "new.txt"}
    assert "-value = 1" in workspace.diff(changes)
    assert "+value = 2" in workspace.diff(changes)
    workspace.restore(changes, undo=True)
    assert source.read_text() == "value = 1\n"
    assert source.stat().st_mode & 0o777 == 0o755
    assert not (workspace.root / "new.txt").exists()
    workspace.restore(changes, undo=False)
    assert source.read_text() == "value = 2\n"
    assert (workspace.root / "new.txt").read_text() == "created\n"


def test_external_changes_are_not_overwritten(workspace: Workspace) -> None:
    source = workspace.root / "main.py"
    source.write_text("value = 1\n")
    workspace.read("main.py")
    source.write_text("value = 1\n# changed by user\n")
    with pytest.raises(CodingError) as error:
        workspace.edit("main.py", "value = 1", "value = 2")
    assert error.value.code == "external_modification"
    assert "# changed by user" in source.read_text()


def test_existing_file_must_be_read_before_replacement(workspace: Workspace) -> None:
    (workspace.root / "main.py").write_text("valuable contents\n")
    with pytest.raises(CodingError) as error:
        workspace.write("main.py", "replacement\n")
    assert error.value.code == "file_not_read"


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        ".env",
        ".env.local",
        "nested/.env",
        ".git/config",
        "node_modules/library.js",
    ],
)
def test_tools_reject_outside_sensitive_and_dependency_paths(
    workspace: Workspace, name: str
) -> None:
    with pytest.raises(CodingError):
        workspace.path(name)


def test_symlinks_are_not_followed_including_broken_links(
    workspace: Workspace, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("outside\n")
    (workspace.root / "link").symlink_to(outside)
    (workspace.root / "broken").symlink_to(tmp_path / "missing")
    for name in ("link", "broken"):
        with pytest.raises(CodingError, match="Symbolic"):
            workspace.path(name, writable=True)
    assert workspace.files() == []


def test_scoped_project_instructions_are_loaded_in_order(workspace: Workspace) -> None:
    (workspace.root / "AGENTS.md").write_text("Root instructions")
    (workspace.root / "src" / "nested").mkdir(parents=True)
    (workspace.root / "src" / "AGENTS.md").write_text("Source instructions")
    (workspace.root / "src" / "nested" / "AGENTS.md").write_text("Nested instructions")
    (workspace.root / "src" / "nested" / "main.py").write_text("value = 1\n")
    result = workspace.read("src/nested/main.py")
    assert [item["content"] for item in result["instructions"]] == [
        "Root instructions",
        "Source instructions",
        "Nested instructions",
    ]


def test_revert_conflict_is_checked_before_restoring_any_file(
    workspace: Workspace,
) -> None:
    workspace.write("a.txt", "old a")
    workspace.write("b.txt", "old b")
    before = workspace.capture()
    workspace.edit("a.txt", "old a", "new a")
    workspace.edit("b.txt", "old b", "new b")
    changes = workspace.changes(before)
    (workspace.root / "b.txt").write_text("user version")
    with pytest.raises(CodingError) as error:
        workspace.restore(changes, undo=True)
    assert error.value.code == "undo_conflict"
    assert (workspace.root / "a.txt").read_text() == "new a"
    assert (workspace.root / "b.txt").read_text() == "user version"


def test_git_ignored_files_are_not_exposed_even_when_listing_is_empty(
    workspace: Workspace,
) -> None:
    subprocess.run(["git", "init", "-q", str(workspace.root)], check=True)
    (workspace.root / ".git" / "info" / "exclude").write_text("*\n")
    (workspace.root / "ignored.txt").write_text("ignored")
    assert workspace.files() == []


def test_configured_path_scope_and_control_files_are_protected(
    workspace: Workspace,
) -> None:
    scoped = Workspace(
        replace(workspace.settings, editable=("src",), protected=("src/private",))
    )
    scoped.write("src/main.py", "ok\n")
    for name in ("README.md", "src/private/key.txt", ".agent-harness.toml"):
        with pytest.raises(CodingError):
            scoped.write(name, "unexpected")


def test_shell_execution_captures_exit_output_and_strips_credentials(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_HARNESS_API_KEY", "test-api-secret")
    result = run_command(
        [
            sys.executable,
            "-c",
            'import os,sys; print(os.getcwd()); print(os.environ.get("AGENT_HARNESS_API_KEY", "absent")); print("error",file=sys.stderr); sys.exit(7)',
        ],
        workspace.root,
        timeout=5,
        stop=threading.Event(),
    )
    assert result["exit_code"] == 7
    assert str(workspace.root) in result["stdout"]
    assert "absent" in result["stdout"]
    assert result["stderr"] == "error\n"
    assert "test-api-secret" not in str(result)


def test_shell_timeout_and_output_limit_are_enforced(workspace: Workspace) -> None:
    result = run_command(
        [
            sys.executable,
            "-c",
            'import time; print("x"*200000,flush=True); time.sleep(30)',
        ],
        workspace.root,
        timeout=0.2,
        stop=threading.Event(),
    )
    assert result["timed_out"] is True
    assert result["truncated"] is True
    assert len(result["stdout"]) <= 64 * 1024
    assert result["duration_seconds"] < 3


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_timeout_reaps_child_even_if_parent_already_exited(
    workspace: Workspace,
) -> None:
    program = """import os,signal,time
pid=os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
else:
    print(pid,flush=True)
"""
    started = time.monotonic()
    result = run_command(
        [sys.executable, "-c", program],
        workspace.root,
        timeout=0.2,
        stop=threading.Event(),
    )
    assert result["timed_out"] is True
    assert time.monotonic() - started < 3


def test_shell_can_be_cancelled_without_waiting_for_timeout(
    workspace: Workspace,
) -> None:
    stop = threading.Event()
    timer = threading.Timer(0.2, stop.set)
    timer.start()
    try:
        result = run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            workspace.root,
            timeout=20,
            stop=stop,
        )
    finally:
        timer.cancel()
    assert result["cancelled"] is True
    assert result["timed_out"] is False
    assert result["duration_seconds"] < 3


def test_diff_reports_empty_files_mode_only_changes_and_missing_newline(workspace):
    before = workspace.capture()
    workspace.write("empty", "")
    assert "new file mode" in workspace.diff(workspace.changes(before))
    workspace.write("mode", "text")
    before = workspace.capture()
    (workspace.root / "mode").chmod(0o755)
    assert "new mode 000755" in workspace.diff(workspace.changes(before))
    workspace.read("mode")
    before = workspace.capture()
    workspace.edit("mode", "text", "changed")
    assert "No newline at end of file" in workspace.diff(workspace.changes(before))


def test_edit_preserves_crlf_line_endings(workspace):
    (workspace.root / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    workspace.read("crlf.txt")
    workspace.edit("crlf.txt", "two", "three")
    assert (workspace.root / "crlf.txt").read_bytes() == b"one\r\nthree\r\n"
