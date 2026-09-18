from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.config import ModelConfig
from vibe.core.tools.builtins.todo import Todo
from vibe.core.types import (
    FileImageSource,
    FunctionCall,
    HardwareVerificationRequiredEvent,
    ImageAttachment,
    Role,
    ToolCall,
)


@pytest.mark.asyncio
async def test_tool_observation_images_are_sent_to_the_next_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = tmp_path / "observation.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    attachment = ImageAttachment(
        source=FileImageSource(path=image_path),
        alias="observation.png",
        mime_type="image/png",
    )
    config = build_test_vibe_config(
        models=[
            ModelConfig(
                name="vision-model",
                provider="mistral",
                alias="vision-model",
                supports_images=True,
            )
        ],
        active_model="vision-model",
        enabled_tools=["todo"],
    )
    tool_call = ToolCall(
        id="observe-1",
        index=0,
        function=FunctionCall(name="todo", arguments='{"action": "read"}'),
    )
    backend = FakeBackend([
        [mock_llm_chunk(content="Observing.", tool_calls=[tool_call])],
        [mock_llm_chunk(content="I can see the observation.")],
    ])
    monkeypatch.setattr(
        Todo, "get_result_images", lambda _self, _result: [attachment], raising=False
    )
    agent = build_test_agent_loop(config=config, backend=backend)

    [_ async for _ in agent.act("inspect the scene")]

    second_request = backend.requests_messages[1]
    injected = [message for message in second_request if message.images]
    assert len(injected) == 1
    assert injected[0].role == Role.user
    assert injected[0].images == [attachment]
    assert "physical observation evidence" in (injected[0].content or "").lower()


@pytest.mark.asyncio
async def test_agent_cannot_end_a_hardware_turn_before_calling_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = build_test_vibe_config(enabled_tools=["todo"])
    tool_call = ToolCall(
        id="physical-action-1",
        index=0,
        function=FunctionCall(name="todo", arguments='{"action": "read"}'),
    )
    backend = FakeBackend([
        [mock_llm_chunk(content="Moving.", tool_calls=[tool_call])],
        [mock_llm_chunk(content="The task is complete.")],
        [mock_llm_chunk(content="Verification failed, so the task is not complete.")],
    ])
    agent = build_test_agent_loop(config=config, backend=backend)
    pending = AsyncMock(side_effect=[{}, {"sim-arm-1": 1}, {}])
    monkeypatch.setattr(
        agent.hardware_runtime, "pending_verification_revisions", pending
    )

    events = [_ async for _ in agent.act("perform the physical task")]

    assert len(backend.requests_messages) == 3
    guard_messages = [
        message
        for message in backend.requests_messages[2]
        if message.role == Role.user and "robo_verify" in (message.content or "")
    ]
    assert len(guard_messages) == 1
    assert any(isinstance(event, HardwareVerificationRequiredEvent) for event in events)
