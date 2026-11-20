"""Responses wire completion must gate actual AgentLoop tool dispatch."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import respx

from tests.agent_loop.e2e.conftest import build_e2e_agent_loop
from tests.agent_loop.e2e.providers import openai_responses
from tests.backend.data.openai_responses import (
    openai_function_call_item,
    openai_response,
    openai_sse,
    openai_text_stream,
)
from vibe.core.agent_loop._generation import IncompleteGenerationError
from vibe.core.llm.exceptions import IncompleteStreamError
from vibe.core.types import ToolResultEvent


@pytest.fixture
def api() -> Iterator[openai_responses.API]:
    with respx.mock(base_url=openai_responses.API.base_url) as router:
        api = openai_responses.API()
        api.setup_router(router)
        yield api


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "status",
    [
        "completed",
        "incomplete",
        "failed",
        "cancelled",
        "in_progress",
        "queued",
        None,
        "tool_calls",
    ],
)
@pytest.mark.parametrize("arguments", ['{"action":"read"}', '{"action":'])
async def test_response_completion_gates_tool_dispatch(
    api: openai_responses.API,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    status: str | None,
    arguments: str,
) -> None:
    item = openai_function_call_item("todo", arguments)
    response = openai_response([item])
    if status is None:
        response.pop("status")
    else:
        response["status"] = status
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    if streaming:
        # Even output_item.done with syntactically valid arguments is not proof
        # that the whole response completed. EOF/[DONE] alone must not dispatch.
        events = [
            {"type": "response.output_item.done", "output_index": 0, "item": item}
        ]
        if status is not None:
            events.append({
                "type": "response.incomplete"
                if status == "incomplete"
                else "response.completed",
                "response": response,
            })
        api.reply_streams(openai_sse(*events, "[DONE]"), openai_text_stream("done"))
    else:
        api.reply(response, openai_responses.Mocks().answer("done"))

    agent = build_e2e_agent_loop(
        config=openai_responses.e2e_config(enabled_tools=["todo"]),
        enable_streaming=streaming,
    )
    dispatched: list[str] = []
    original = agent._run_tools_concurrently

    async def capture(calls):
        dispatched.extend(call.call_id for call in calls)
        async for event in original(calls):
            yield event

    monkeypatch.setattr(agent, "_run_tools_concurrently", capture)
    if status == "completed" and arguments == '{"action":"read"}':
        events = [event async for event in agent.act("Read my todo list.")]
        assert len(dispatched) == 1
        assert sum(isinstance(event, ToolResultEvent) for event in events) == 1
        assert api.route.call_count == 2
    else:
        with pytest.raises((IncompleteGenerationError, IncompleteStreamError)):
            [_ async for _ in agent.act("Read my todo list.")]
        assert dispatched == []
        assert api.route.call_count == 1
