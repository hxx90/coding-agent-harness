from __future__ import annotations

import shutil
import sys
from dataclasses import replace

import pytest

from agent_harness.config import WorkspaceConfig
from agent_harness.snapshot import SnapshotManager
from agent_harness.tools import ToolExecutor
from agent_harness.trace import Redactor
from agent_harness.validation import ValidationRunner
from agent_harness.workspace import create_isolated_workspace


def build_tools(workspace, task_dir):
    config = WorkspaceConfig.load(str(workspace))
    snapshot = SnapshotManager(config, task_dir)
    snapshot.create()
    runner = ValidationRunner(config)
    return config, snapshot, ToolExecutor(config, snapshot, runner)


def test_read_only_and_sensitive_permissions(workspace_factory, tmp_path):
    workspace = workspace_factory("e01_bugfix")
    (workspace / ".env").write_text("API_KEY=dummy\n", encoding="utf-8")
    config, _, tools = build_tools(workspace, tmp_path / "task")

    assert config.can_read("tests/test_user.py")
    assert not config.can_write("tests/test_user.py")
    assert not config.can_read(".env")

    read_test, _ = tools.execute(
        "read_file",
        {"path": "tests/test_user.py", "start_line": 1, "end_line": 20},
    )
    assert read_test.ok

    read_secret, _ = tools.execute("read_file", {"path": ".env"})
    assert not read_secret.ok
    assert read_secret.error_code == "path_sensitive"

    patch_test = """--- a/tests/test_user.py
+++ b/tests/test_user.py
@@ -1,1 +1,1 @@
-import pytest
+import pytest  # changed
"""
    write_test, _ = tools.execute("apply_patch", {"patch": patch_test})
    assert not write_test.ok
    assert write_test.error_code == "path_protected"


def test_snapshot_diff_and_revert_preserve_pre_task_change(workspace_factory, tmp_path):
    workspace = workspace_factory("e01_bugfix")
    user_file = workspace / "src" / "user.py"
    user_file.write_text(
        "# user change before agent task\n" + user_file.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _, snapshot, tools = build_tools(workspace, tmp_path / "task")

    patch_existing = """--- a/src/user.py
+++ b/src/user.py
@@ -1,5 +1,5 @@
 # user change before agent task
 def validate_username(username: str | None) -> str:
-    if username is None:
+    if username is None or not username.strip():
         raise ValueError("username is required")
     return username
"""
    result, _ = tools.execute("apply_patch", {"patch": patch_existing})
    assert result.ok

    patch_new = """--- /dev/null
+++ b/src/helper.py
@@ -0,0 +1,1 @@
+VALUE = 1
"""
    result, _ = tools.execute("apply_patch", {"patch": patch_new})
    assert result.ok
    assert "src/helper.py" in snapshot.diff()

    reverted = snapshot.revert()
    assert reverted.ok
    assert not (workspace / "src" / "helper.py").exists()
    restored = user_file.read_text(encoding="utf-8")
    assert restored.startswith("# user change before agent task\n")
    assert "not username.strip()" not in restored


def test_patch_outside_workspace_is_rejected(workspace_factory, tmp_path):
    workspace = workspace_factory("e01_bugfix")
    _, _, tools = build_tools(workspace, tmp_path / "task")
    patch = """--- /dev/null
+++ b/../outside.py
@@ -0,0 +1,1 @@
+VALUE = 1
"""
    result, _ = tools.execute("apply_patch", {"patch": patch})
    assert not result.ok
    assert result.error_code == "path_outside_workspace"
    assert not (workspace.parent / "outside.py").exists()


def test_redactor_removes_known_and_inline_secrets():
    redactor = Redactor(["testtoken"])
    text = "Authorization: Bearer testtoken API_KEY=x"
    output = redactor.text(text)
    assert "testtoken" not in output
    assert "API_KEY=x" not in output
    assert "[REDACTED]" in output


def test_patch_reports_normalized_counts_and_rejects_noop(
    workspace_factory, tmp_path
):
    workspace = workspace_factory("e02_feature")
    _, _, tools = build_tools(workspace, tmp_path / "task")
    wrong_counts = '''--- a/src/order.py
+++ b/src/order.py
@@ -1,99 +1,42 @@
 """Order domain helpers."""
+
+VALUE = 1
'''
    result, _ = tools.execute("apply_patch", {"patch": wrong_counts})
    assert result.ok
    assert result.data["normalized_hunk_counts"] == [
        {
            "path": "src/order.py",
            "hunk": 1,
            "declared_old_count": 99,
            "actual_old_count": 1,
            "declared_new_count": 42,
            "actual_new_count": 3,
        }
    ]

    off_by_two_lines = '''--- a/src/order.py
+++ b/src/order.py
@@ -1,1 +1,1 @@
-VALUE = 1
+VALUE = 2
'''
    relocated, _ = tools.execute("apply_patch", {"patch": off_by_two_lines})
    assert relocated.ok
    assert "VALUE = 2" in (workspace / "src" / "order.py").read_text(
        encoding="utf-8"
    )

    noop = '''--- a/src/order.py
+++ b/src/order.py
@@ -1 +1 @@
-"""Order domain helpers."""
+"""Order domain helpers."""
'''
    result, _ = tools.execute("apply_patch", {"patch": noop})
    assert not result.ok
    assert result.error_code == "patch_no_changes"


def test_patch_accepts_model_blank_lines_without_diff_prefix(
    workspace_factory, tmp_path
):
    workspace = workspace_factory("e02_feature")
    _, _, tools = build_tools(workspace, tmp_path / "task")
    existing_patch = '''--- a/src/order.py
+++ b/src/order.py
@@ -1,2 +1,3 @@
 """Order domain helpers."""

+VALUE = 1
'''
    existing_result, _ = tools.execute(
        "apply_patch", {"patch": existing_patch}
    )
    assert existing_result.ok
    assert "VALUE = 1" in (workspace / "src" / "order.py").read_text(
        encoding="utf-8"
    )

    new_file_patch = '''--- /dev/null
+++ b/src/generated.py
@@ -0,0 +1,3 @@
+FIRST = 1

+LAST = 2
'''
    new_result, _ = tools.execute("apply_patch", {"patch": new_file_patch})
    assert new_result.ok
    assert (workspace / "src" / "generated.py").read_text(encoding="utf-8") == (
        "FIRST = 1\n\nLAST = 2\n"
    )


def test_write_file_replace_append_permissions_and_revert(
    workspace_factory, tmp_path
):
    workspace = workspace_factory("e01_bugfix")
    _, snapshot, tools = build_tools(workspace, tmp_path / "task")

    created, _ = tools.execute(
        "write_file",
        {"path": "src/generated.py", "content": "VALUE = 1\n"},
    )
    assert created.ok
    assert created.data["operation"] == "written"

    appended, _ = tools.execute(
        "write_file",
        {
            "path": "src/generated.py",
            "content": "EXTRA = 2\n",
            "mode": "append",
        },
    )
    assert appended.ok
    assert appended.data["operation"] == "appended"
    assert appended.data["inserted_separator"] is False
    assert (workspace / "src" / "generated.py").read_text(encoding="utf-8") == (
        "VALUE = 1\nEXTRA = 2\n"
    )

    no_newline, _ = tools.execute(
        "write_file",
        {"path": "src/chunks.js", "content": "})();"},
    )
    assert no_newline.ok
    next_chunk, _ = tools.execute(
        "write_file",
        {
            "path": "src/chunks.js",
            "content": "(function () {})();",
            "mode": "append",
        },
    )
    assert next_chunk.ok
    assert next_chunk.data["inserted_separator"] is True
    assert (workspace / "src" / "chunks.js").read_text(encoding="utf-8") == (
        "})();\n(function () {})();"
    )

    protected, _ = tools.execute(
        "write_file",
        {"path": "tests/test_user.py", "content": "blocked\n"},
    )
    assert not protected.ok
    assert protected.error_code == "path_protected"

    unchanged, _ = tools.execute(
        "write_file",
        {
            "path": "src/generated.py",
            "content": "VALUE = 1\nEXTRA = 2\n",
        },
    )
    assert not unchanged.ok
    assert unchanged.error_code == "write_no_changes"

    too_large, _ = tools.execute(
        "write_file",
        {"path": "src/generated.py", "content": "x" * 12_001},
    )
    assert not too_large.ok
    assert too_large.error_code == "write_chunk_too_large"
    assert too_large.data["max_chars"] == 12_000
    assert "src/generated.py" in snapshot.diff()

    reverted = snapshot.revert()
    assert reverted.ok
    assert not (workspace / "src" / "generated.py").exists()
    assert not (workspace / "src" / "chunks.js").exists()


def test_edit_file_handles_long_lines_and_requires_unique_exact_match(
    workspace_factory, tmp_path
):
    workspace = workspace_factory("e01_bugfix")
    long_line = 'const banner = "' + ("x" * 4_500) + 'BROKEN' + ("y" * 100) + '";\n'
    target = workspace / "src" / "chunks.js"
    target.write_text(long_line + "const repeated = 1;\nconst repeated = 1;\n", encoding="utf-8")
    _, snapshot, tools = build_tools(workspace, tmp_path / "task")

    edited, _ = tools.execute(
        "edit_file",
        {
            "path": "src/chunks.js",
            "old_string": "xxxBROKENyyy",
            "new_string": "xxxFIXEDyyy",
        },
    )
    assert edited.ok
    assert edited.data["operation"] == "edited"
    assert edited.data["matched_occurrences"] == 1
    assert "xxxFIXEDyyy" in target.read_text(encoding="utf-8")
    assert "src/chunks.js" in snapshot.changed_files()

    ambiguous, _ = tools.execute(
        "edit_file",
        {
            "path": "src/chunks.js",
            "old_string": "const repeated = 1;",
            "new_string": "const repeated = 2;",
        },
    )
    assert not ambiguous.ok
    assert ambiguous.error_code == "edit_ambiguous"
    assert ambiguous.data["occurrences"] == 2

    protected, _ = tools.execute(
        "edit_file",
        {
            "path": "tests/test_user.py",
            "old_string": "import pytest",
            "new_string": "import pytest  # changed",
        },
    )
    assert not protected.ok
    assert protected.error_code == "path_protected"


def test_history_summary_marker_cannot_be_added_but_can_be_removed(
    workspace_factory, tmp_path
):
    workspace = workspace_factory("e01_bugfix")
    source = workspace / "src" / "user.py"
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n[HARNESS_COMPACTED legacy history summary]\n",
        encoding="utf-8",
    )
    _, _, tools = build_tools(workspace, tmp_path / "task")

    rejected_write, _ = tools.execute(
        "write_file",
        {
            "path": "src/other.py",
            "content": "[HARNESS_COMPACTED copied into code]",
        },
    )
    assert not rejected_write.ok
    assert rejected_write.error_code == "compacted_history_reuse"

    rejected_patch, _ = tools.execute(
        "apply_patch",
        {
            "patch": """--- a/src/user.py
+++ b/src/user.py
@@ -1,1 +1,2 @@
 def validate_username(username: str | None) -> str:
+[HARNESS_COMPACTED copied into code]
"""
        },
    )
    assert not rejected_patch.ok
    assert rejected_patch.error_code == "compacted_history_reuse"

    cleanup, _ = tools.execute(
        "apply_patch",
        {
            "patch": """--- a/src/user.py
+++ b/src/user.py
@@ -5,2 +5,1 @@
 
-[HARNESS_COMPACTED legacy history summary]
"""
        },
    )
    assert cleanup.ok
    assert "HARNESS_COMPACTED" not in source.read_text(encoding="utf-8")


def test_ambiguous_python_command_uses_harness_interpreter(
    workspace_factory,
):
    workspace = workspace_factory(
        "e01_bugfix",
        validation_command=["python", "-m", "pytest", "-q"],
    )
    config = WorkspaceConfig.load(str(workspace))
    assert config.validation_command[0] == sys.executable


def test_autonomous_copy_opens_engineering_code_but_preserves_boundaries(
    workspace_factory,
):
    source = workspace_factory("e01_bugfix")
    (source / ".env").write_text("API_KEY=dummy\n", encoding="utf-8")
    source_config = WorkspaceConfig.load(str(source))
    isolated = create_isolated_workspace(source_config)
    config = WorkspaceConfig.load(str(isolated)).for_autonomous_copy()

    assert isolated != source
    assert (isolated / "src" / "user.py").is_file()
    assert not (isolated / ".env").exists()
    assert config.can_write("src/user.py")
    assert config.can_write("README.md")
    assert config.can_write("new_engineering_file.py")
    assert not config.can_write("tests/test_user.py")
    assert not config.can_write(".agent-harness.json")
    assert not config.can_read(".env")


def test_autonomous_validation_cannot_write_workspace(workspace_factory, tmp_path):
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        pytest.skip("autonomous validation sandbox is a macOS MVP boundary")
    workspace = workspace_factory("e01_bugfix")
    config = replace(
        WorkspaceConfig.load(str(workspace)),
        validation_command=(
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "Path('src/validation_escape.txt').write_text('blocked')"
            ),
        ),
    )
    runner = ValidationRunner(
        config,
        sandboxed=True,
        scratch_dir=tmp_path / "validation-scratch",
    )

    result = runner.run()

    assert result.exit_code not in (None, 0)
    assert not (workspace / "src" / "validation_escape.txt").exists()


@pytest.mark.parametrize(
    "probe",
    [
        "import socket; socket.create_connection(('example.com', 80), timeout=1)",
        "import subprocess; subprocess.run(['/bin/echo', 'unexpected'], check=True)",
    ],
    ids=["network", "child-process"],
)
def test_autonomous_validation_cannot_use_network_or_child_process(
    workspace_factory, tmp_path, probe
):
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        pytest.skip("autonomous validation sandbox is a macOS MVP boundary")
    workspace = workspace_factory("e01_bugfix")
    config = replace(
        WorkspaceConfig.load(str(workspace)),
        validation_command=(sys.executable, "-c", probe),
    )
    runner = ValidationRunner(
        config,
        sandboxed=True,
        scratch_dir=tmp_path / "validation-scratch",
    )

    result = runner.run()

    assert result.exit_code not in (None, 0)
    assert "unexpected\n" not in result.stdout
