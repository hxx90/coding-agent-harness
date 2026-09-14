from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace

import pytest

from agent_harness.coding.permissions import Permissions
from agent_harness.coding.settings import ProviderSettings, Settings
from agent_harness.coding.tools import ToolSet
from agent_harness.coding.types import CodingError
from agent_harness.coding.workspace import Workspace


@pytest.fixture
def tools(tmp_path):
    settings = Settings(tmp_path, tmp_path.parent / "tool-data", ProviderSettings())
    return ToolSet(
        settings,
        Workspace(settings),
        Permissions([{"tool": "*", "action": "allow"}]),
        {"todos": []},
        threading.Event(),
    )


def execute(tools, name, **args):
    return tools.execute(name, json.dumps(args))


def test_patch_search_and_checklist_use_actual_project(tools):
    execute(tools, "write", path="main.py", content="value = 1\n")
    assert execute(tools, "glob", pattern="*.py")["files"] == ["main.py"]
    assert execute(tools, "grep", query="value")["matches"][0]["line"] == 1
    execute(tools, "read", path="main.py")
    patch = "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    assert execute(tools, "apply_patch", patch=patch)["changed"]
    assert (tools.settings.project / "main.py").read_text() == "value = 2\n"
    todos = [{"id": "test", "content": "Run tests", "status": "in_progress"}]
    execute(tools, "todo_write", todos=todos)
    assert execute(tools, "todo_read")["todos"] == todos
    with pytest.raises(CodingError):
        execute(tools, "todo_write", todos=todos * 2)


@pytest.mark.parametrize(
    "name,args",
    [
        ("read", {"path": 5}),
        ("read", {}),
        ("read", {"path": "x", "extra": True}),
        ("todo_write", {"todos": {}}),
        ("bash", {"command": "true", "description": "test", "timeout": True}),
    ],
)
def test_invalid_arguments_are_rejected_before_operation(tools, name, args):
    with pytest.raises(CodingError) as exc:
        execute(tools, name, **args)
    assert exc.value.code == "invalid_arguments"


def test_validation_cannot_pass_if_it_rewrites_project(tools):
    settings = replace(
        tools.settings,
        validation_command=(sys.executable, "-c", "open('mutated','w').write('bad')"),
    )
    runner = ToolSet(
        settings, tools.workspace, tools.permissions, tools.state, tools.stop
    )
    result = execute(runner, "test")
    assert result["exit_code"] == 0 and result["passed"] is False
    assert result["workspace_changed_during_validation"] is True


def test_permission_preview_contains_exact_file_change_and_grant_is_not_reused(tools):
    approvals = []

    def approve(name, details):
        approvals.append((name, details))
        return "session"

    tools.permissions = Permissions(approve=approve)
    execute(tools, "write", path="x", content="first")
    execute(tools, "write", path="x", content="second")
    assert len(approvals) == 2
    assert '"content": "first"' in approvals[0][1]
    assert '"content": "second"' in approvals[1][1]


def test_sensitive_agents_file_is_not_loaded_as_instructions(tools):
    (tools.settings.project / "AGENTS.md").write_text("confidential instruction")
    workspace = Workspace(replace(tools.settings, sensitive=("AGENTS.md",)))
    assert workspace.instructions() == []


def test_public_configuration_masks_mcp_environment_values(tools):
    settings = replace(
        tools.settings, mcp={"server": {"env": {"CUSTOM": "private-value"}}}
    )
    assert "private-value" not in json.dumps(settings.public())
