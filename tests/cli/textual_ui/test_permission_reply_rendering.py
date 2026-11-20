from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_app, wait_until
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.cli.textual_ui.widgets.approval_app import ApprovalApp
from vibe.cli.textual_ui.widgets.chat_input.container import ChatInputContainer
from vibe.cli.textual_ui.widgets.messages import AssistantMessage
from vibe.core.types import FunctionCall, ToolCall


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["y", "n"])
async def test_reply_is_rendered_after_permission(decision: str, tmp_path: Path):
    marker = tmp_path / "marker.txt"
    reply = "VISIBLE-REPLY-CANARY"
    backend = FakeBackend([
        [
            mock_llm_chunk(
                content="",
                tool_calls=[
                    ToolCall(
                        id="permission-test",
                        function=FunctionCall(
                            name="bash",
                            arguments=json.dumps({
                                "command": f"printf done > {marker}"
                            }),
                        ),
                    )
                ],
                stop_reason="tool_calls",
            )
        ],
        [mock_llm_chunk(content=reply)],
    ])
    agent = build_test_agent_loop(backend=backend, enable_streaming=True)
    app = build_test_vibe_app(agent_loop=agent)
    async with app.run_test(size=(120, 36)) as pilot:
        await app._ensure_runtime_ready()
        app.query_one(ChatInputContainer).post_message(
            ChatInputContainer.Submitted("Run the marker command")
        )
        assert await wait_until(pilot, lambda: bool(app.query(ApprovalApp)), timeout=5)
        assert await wait_until(
            pilot,
            lambda: (
                app.query_one(ApprovalApp).has_focus
                and not app.query_one(ApprovalApp).is_within_grace_period()
            ),
        )
        await pilot.press(decision)
        if decision == "n":
            assert await wait_until(
                pilot, lambda: not app._agent_job_active(), timeout=5
            )
            app.query_one(ChatInputContainer).post_message(
                ChatInputContainer.Submitted("Next turn")
            )
        assert await wait_until(
            pilot,
            lambda: any(reply == m.get_content() for m in app.query(AssistantMessage)),
            timeout=5,
        )
        assert await wait_until(pilot, lambda: not app._agent_job_active(), timeout=5)
        await pilot.pause()
        app.save_screenshot(f"permission-{decision}.svg", path=str(tmp_path))
        assert reply in app.export_screenshot(), (
            "stored message was not visibly rendered"
        )
        assert marker.exists() == (decision == "y")
