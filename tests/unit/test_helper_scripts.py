from __future__ import annotations

import shutil
import sys

import pytest

from agent_harness.config import WorkspaceConfig
from agent_harness.helper_scripts import HelperScriptRunner


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="helper scripts require the macOS process sandbox",
)


def test_helper_script_reads_workspace_but_cannot_write_or_read_sensitive(
    workspace_factory, tmp_path
):
    workspace = workspace_factory("e01_bugfix")
    (workspace / ".env").write_text("API_KEY=dummy\n", encoding="utf-8")
    config = WorkspaceConfig.load(str(workspace)).for_autonomous_copy()
    runner = HelperScriptRunner(config, tmp_path / "helpers")

    created = runner.write(
        "inspect",
        "from pathlib import Path\nprint(Path('src/user.py').read_text())\n",
    )
    assert created["name"] == "inspect.py"
    assert created["replaced"] is False
    result = runner.run("inspect", [], 5)
    assert result["exit_code"] == 0
    assert "validate_username" in result["stdout"]

    runner.write(
        "try_write",
        "from pathlib import Path\nPath('src/forbidden.py').write_text('x')\n",
    )
    result = runner.run("try_write", [], 5)
    assert result["exit_code"] != 0
    assert not (workspace / "src" / "forbidden.py").exists()

    runner.write(
        "try_secret",
        "from pathlib import Path\nprint(Path('.env').read_text())\n",
    )
    result = runner.run("try_secret", [], 5)
    assert result["exit_code"] != 0
    assert "dummy" not in result["stdout"]


def test_helper_script_cannot_use_network_or_child_process(
    workspace_factory, tmp_path
):
    workspace = workspace_factory("e01_bugfix")
    config = WorkspaceConfig.load(str(workspace)).for_autonomous_copy()
    runner = HelperScriptRunner(config, tmp_path / "helpers")

    runner.write(
        "network",
        "import socket\nsocket.create_connection(('example.com', 80), timeout=1)\n",
    )
    assert runner.run("network", [], 5)["exit_code"] != 0

    runner.write(
        "child",
        "import subprocess\nsubprocess.run(['/bin/echo', 'unexpected'], check=True)\n",
    )
    result = runner.run("child", [], 5)
    assert result["exit_code"] != 0
    assert "unexpected\n" not in result["stdout"]


def test_helper_script_storage_evicts_old_artifact_at_capacity(
    workspace_factory, tmp_path
):
    workspace = workspace_factory("e01_bugfix")
    config = WorkspaceConfig.load(str(workspace)).for_autonomous_copy()
    helper_dir = tmp_path / "helpers"
    runner = HelperScriptRunner(config, helper_dir)

    for index in range(10):
        runner.write("inspect_%s" % index, "print(%s)\n" % index)
    created = runner.write("inspect_latest", "print('latest')\n")

    assert created["evicted"] is not None
    assert (helper_dir / "inspect_latest.py").is_file()
    assert len(list(helper_dir.glob("*.py"))) == 10
