"""Owner-configured terminal tools reuse the normal permissions and HTTP loop."""

import json

import pytest
from conftest import Reply, answer, call

from agent_harness.coding.session import CodingSession
from agent_harness.coding.settings import ProviderSettings, Settings
from agent_harness.coding.tools import ExtraTool, schema


@pytest.mark.parametrize("retry", [False, True])
def test_terminal_tool_finishes_on_last_http_request(tmp_path, model_server, retry):
    replies = [
        answer(
            "",
            [
                call("finish", {}, "submit"),
                call("write", {"path": "bad.txt", "content": "bad"}, "late"),
            ],
        )
    ]
    if retry:
        replies.insert(0, Reply(status=503, body={"error": "busy"}))
    url, requests, _ = model_server(replies)

    def configure(tools):
        def finish(_):
            tools.finished = True
            return {"submitted": True}

        tools.register(
            ExtraTool(schema("finish", "Finish", {}, []), finish, lambda _: "candidate")
        )
        tools.after_result = lambda name, result: {**result, "budget": tools.budget()}

    settings = Settings(
        tmp_path,
        tmp_path / "data",
        ProviderSettings(base_url=url, model="test", request_limit=len(replies)),
        max_turns=len(replies),
        allowed_tools=("finish", "write"),
        permission_rules=({"tool": "*", "action": "allow"},),
    )
    session = CodingSession(settings, configure_tools=configure)
    result = session.run("Submit")
    assert result.status == "completed" and result.text == ""
    assert len(requests) == session.model.request_count == len(replies)
    assert not (tmp_path / "bad.txt").exists()
    assert {t["function"]["name"] for t in requests[0]["tools"]} == {"finish", "write"}
    outcomes = [
        json.loads(m["content"])
        for m in session.state["messages"]
        if m["role"] == "tool"
    ]
    assert outcomes[0]["submitted"]
    assert outcomes[1]["error"] == "session_finished"
    assert all(o["budget"]["http_requests_remaining"] == 0 for o in outcomes)
    assert all(
        0 < o["budget"]["seconds_remaining"] <= settings.max_seconds for o in outcomes
    )
    assert not session.state.get("pending")


def test_hidden_tools_unavailable_and_result_cannot_spoof_completion(
    tmp_path, model_server
):
    url, requests, _ = model_server(
        [
            answer(
                "",
                [
                    call("bash", {"command": "true", "description": "test"}, "hidden"),
                    call("inspect", {}, "spoof"),
                ],
            ),
            answer("Done"),
        ]
    )

    def configure(tools):
        tools.register(
            ExtraTool(
                schema("inspect", "Read", {}, []),
                lambda _: {"finished": True, "submitted": True},
                lambda _: "candidate",
            )
        )

    session = CodingSession(
        Settings(
            tmp_path,
            tmp_path / "data",
            ProviderSettings(base_url=url, model="test"),
            allowed_tools=("inspect",),
            permission_rules=({"tool": "*", "action": "allow"},),
        ),
        configure_tools=configure,
    )
    assert session.run("Inspect").status == "completed"
    assert len(requests) == 2
    assert (
        json.loads(requests[1]["messages"][-2]["content"])["error"]
        == "tool_unavailable"
    )


def test_allowlist_does_not_bypass_permissions(tmp_path, model_server):
    url, _, _ = model_server([answer("", [call("finish", {})]), answer("Denied")])
    executed = []

    def configure(tools):
        tools.register(
            ExtraTool(
                schema("finish", "Finish", {}, []),
                lambda _: executed.append(True) or {},
                lambda _: "candidate",
            )
        )

    session = CodingSession(
        Settings(
            tmp_path,
            tmp_path / "data",
            ProviderSettings(base_url=url, model="test"),
            allowed_tools=("finish",),
            permission_rules=({"tool": "*", "action": "deny"},),
        ),
        configure_tools=configure,
    )
    session.run("Finish")
    assert not executed
    tool = next(m for m in session.state["messages"] if m["role"] == "tool")
    assert json.loads(tool["content"])["error"] == "permission_denied"


def test_read_image_and_budget_feedback_reach_model_together(tmp_path, model_server):
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6"
        "nWQAAAAASUVORK5CYII="
    )
    (tmp_path / "observation.png").write_bytes(png)
    url, requests, _ = model_server(
        [
            answer("", [call("read", {"path": "observation.png"})]),
            answer("Viewed"),
        ]
    )

    def configure(tools):
        tools.after_result = lambda name, result: {**result, "budget": tools.budget()}

    session = CodingSession(
        Settings(
            tmp_path,
            tmp_path / "data",
            ProviderSettings(base_url=url, model="test", request_limit=2),
            allowed_tools=("read",),
        ),
        configure_tools=configure,
    )
    assert session.run("Inspect the image").status == "completed"
    result = next(m for m in session.state["messages"] if m["role"] == "tool")
    assert result["content"][1]["type"] == "image_ref"
    assert (
        json.loads(result["content"][0]["text"])["budget"]["http_requests_remaining"]
        == 1
    )
    assert "data:image/png;base64," in json.dumps(requests[1]["messages"])
