from __future__ import annotations

import sys

import pytest
from conftest import answer, call

from agent_harness.coding.extensions import Skills
from agent_harness.coding.session import CodingSession
from agent_harness.coding.settings import ProviderSettings, Settings
from agent_harness.coding.types import CodingError

MCP_SERVER = """import json,sys,time
for line in sys.stdin:
    request=json.loads(line)
    method=request.get('method')
    if 'id' not in request: continue
    if method=='initialize':
        result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
    elif method=='tools/list':
        result={'tools':[{'name':'echo','description':'Echo text','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}]}
    elif method=='tools/call':
        result={'content':[{'type':'text','text':'MCP '+request['params']['arguments']['text']}]}
    else:
        result={}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
"""


def setup(tmp_path, model_server, responses, **kwargs):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    url, requests, _ = model_server(responses)
    settings = Settings(
        project,
        tmp_path / "data",
        ProviderSettings(base_url=url, model="test-model"),
        **kwargs,
    )
    return CodingSession(settings), requests


def test_skill_discovery_loading_and_confined_resources(tmp_path):
    folder = tmp_path / "skills" / "local"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        '---\nname: local\ndescription: "Useful local skill"\n---\nRead the project and use type hints.\n'
    )
    (folder / "guide.md").write_text("Detailed guide")
    skills = Skills((folder.parent,))
    assert skills.catalog() == [{"name": "local", "description": "Useful local skill"}]
    assert "type hints" in skills.load({"name": "local"})["content"]
    assert (
        skills.load({"name": "local", "resource": "guide.md"})["content"]
        == "Detailed guide"
    )
    with pytest.raises(CodingError):
        skills.load({"name": "local", "resource": "../outside"})
    (folder / "link").symlink_to(tmp_path)
    with pytest.raises(CodingError):
        skills.load({"name": "local", "resource": "link/x"})


def test_skill_tool_is_available_to_real_model_loop_in_plan(tmp_path, model_server):
    folder = tmp_path / "skills" / "local"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("Review naming conventions")
    session, requests = setup(
        tmp_path,
        model_server,
        [answer("", [call("skill", {"name": "local"})]), answer("Skill followed")],
        mode="plan",
        skill_dirs=(folder.parent,),
    )
    assert session.run("Use local skill").status == "completed"
    assert "Review naming conventions" in requests[1]["messages"][-1]["content"]


def test_child_exploration_is_persistent_restricted_and_charged_to_parent(
    tmp_path, model_server
):
    responses = [
        answer("", [call("task", {"prompt": "Inspect language"})]),
        answer("", [call("glob", {})]),
        answer("Child found Python"),
        answer("Parent report"),
    ]
    session, requests = setup(tmp_path, model_server, responses)
    (session.settings.project / "main.py").write_text("x = 1")
    result = session.run("Explore project")
    assert result.status == "completed" and result.turns == 4
    child_names = {tool["function"]["name"] for tool in requests[1]["tools"]}
    assert not child_names & {"task", "write", "bash", "test"}
    children = [s for s in session.store.list() if s["id"] != session.id]
    assert len(children) == 1
    assert session.store.load(children[0]["id"])["mode"] == "plan"
    assert result.usage["total_tokens"] == 60
    assert "Child found Python" in requests[-1]["messages"][-1]["content"]


def test_real_mcp_process_initializes_lists_calls_and_is_reaped(tmp_path, model_server):
    script = tmp_path / "mcp.py"
    script.write_text(MCP_SERVER)
    session, requests = setup(
        tmp_path,
        model_server,
        [
            answer("", [call("mcp_fixture_echo", {"text": "hello"})]),
            answer("Received echo"),
        ],
        permission_rules=({"tool": "*", "action": "allow"},),
        mcp={"fixture": {"command": [sys.executable, str(script)]}},
    )
    assert session.run("Call echo").status == "completed"
    assert "MCP hello" in requests[1]["messages"][-1]["content"]
    assert any(
        t["function"]["name"] == "mcp_fixture_echo" for t in requests[0]["tools"]
    )


def test_mcp_start_is_separately_permission_controlled(tmp_path, model_server):
    marker = tmp_path / "started"
    session, requests = setup(
        tmp_path,
        model_server,
        [],
        mcp={
            "fixture": {
                "command": [
                    sys.executable,
                    "-c",
                    f"open({str(marker)!r},'w').write('started')",
                ]
            }
        },
    )
    result = session.run("Use tools")
    assert result.status == "permission_required"
    assert not marker.exists() and requests == []


def test_mcp_tools_require_permission_after_startup_approval(tmp_path, model_server):
    script = tmp_path / "mcp.py"
    script.write_text(MCP_SERVER)
    session, _ = setup(
        tmp_path,
        model_server,
        [answer("", [call("mcp_fixture_echo", {"text": "hello"})])],
        permission_rules=({"tool": "mcp_start", "action": "allow"},),
        mcp={"fixture": {"command": [sys.executable, str(script)]}},
    )
    result = session.run("Call echo")
    assert result.status == "permission_required"
    assert "mcp_fixture_echo" in result.error


@pytest.mark.parametrize(
    "source,code",
    [
        ("print('invalid',flush=True)", "mcp_protocol"),
        ("import time; time.sleep(5)", "mcp_timeout"),
    ],
)
def test_mcp_failures_are_bounded_and_reported(tmp_path, model_server, source, code):
    session, requests = setup(
        tmp_path,
        model_server,
        [],
        permission_rules=({"tool": "*", "action": "allow"},),
        mcp={"fixture": {"command": [sys.executable, "-c", source], "timeout": 0.15}},
    )
    result = session.run("Use MCP")
    assert result.status == "error" and requests == []
    events = (session.store.root / (session.id + ".jsonl")).read_text()
    assert code in events
