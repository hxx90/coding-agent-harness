from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from tests.conftest import build_test_vibe_config
from tests.mock.mock_backend_factory import mock_backend_factory
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.app_server.local import ClientDescriptor, LocalHarnessOptions
from vibe.app_server.models import (
    PublicCallbackEntry,
    PublicEntryGenerationStatus,
    PublicMessageEntry,
    TextContentBlock,
)
from vibe.app_server.protocol import ClientInfo, SessionOptions
from vibe.capx import CapXCompletionRequest, CapXMessage
from vibe.capx.harness_agent import CapXHarnessAgent
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.config import VibeConfigSchema
from vibe.core.config.harness_files import HarnessFilesManager
from vibe.core.config.layers.overrides import OverridesLayer
from vibe.core.config.orchestrator import ConfigOrchestrator
from vibe.core.types import Backend


class FakeSession:
    def __init__(self) -> None:
        self.prompt: str | None = None
        self.images = []
        self.closed = False
        self.history = [
            PublicMessageEntry(
                id="answer",
                session_id="session",
                turn_id="turn",
                role="assistant",
                content=[TextContentBlock(text="```python\nprint('ok')\n```")],
                generation_status=PublicEntryGenerationStatus.COMPLETED,
                created_at=1,
                updated_at=1,
            )
        ]

    async def act(self, message: str, *, images=None) -> AsyncGenerator[object, None]:
        self.prompt = message
        self.images = images or []
        if False:
            yield object()

    async def close(self) -> None:
        self.closed = True

    async def deny_callback(self, callback: PublicCallbackEntry) -> None:
        raise AssertionError(f"Unexpected callback: {callback}")


@pytest.mark.asyncio
async def test_harness_agent_forwards_history_and_inline_images() -> None:
    session = FakeSession()

    async def start_session() -> FakeSession:
        return session

    agent = CapXHarnessAgent(start_session)
    request = CapXCompletionRequest.model_validate({
        "model": "robo-harness",
        "messages": [
            {"role": "system", "content": "Only Python."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Solve it."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                    },
                ],
            },
            {"role": "assistant", "content": "bad code"},
            {"role": "user", "content": "stderr: NameError"},
        ],
    })

    response = await agent.complete(request, "run-1")

    assert response == "```python\nprint('ok')\n```"
    assert session.prompt is not None
    assert "[system]\nOnly Python." in session.prompt
    assert "[assistant]\nbad code" in session.prompt
    assert "[user]\nstderr: NameError" in session.prompt
    assert len(session.images) == 1
    assert session.images[0].source.data == "aW1hZ2U="
    assert session.closed is True


@pytest.mark.asyncio
async def test_harness_agent_runs_through_the_public_app_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = build_test_vibe_config(
        include_model_info=False, include_commit_signature=False
    )

    async def build_orchestrator(
        data: dict[str, object] | None = None, *, harness_files: HarnessFilesManager
    ) -> ConfigOrchestrator[VibeConfigSchema]:
        del harness_files
        base = OverridesLayer(data=config.model_dump(mode="json"), name="base")
        session = OverridesLayer(data=data or {})
        return await ConfigOrchestrator.create(
            schema=VibeConfigSchema,
            layers=[base, session],
            default_layer_resolver=lambda: base,
        )

    monkeypatch.setattr(
        "vibe.app_server._runtime.build_default_orchestrator", build_orchestrator
    )
    options = LocalHarnessOptions(
        client=ClientDescriptor(
            info=ClientInfo(
                name="robo_capx_test", version="test", entrypoint="programmatic"
            )
        ),
        session_options=SessionOptions(
            agent=BuiltinAgentName.AUTO_APPROVE,
            enabled_tools=["read_file"],
            headless=True,
        ),
    )
    request = CapXCompletionRequest(
        model="robo-harness",
        messages=[CapXMessage(role="user", content="Write Python")],
    )

    with mock_backend_factory(
        Backend.MISTRAL,
        lambda provider, **kwargs: FakeBackend([
            mock_llm_chunk(content="```python\nprint('real harness')\n```")
        ]),
    ):
        response = await CapXHarnessAgent.local(options).complete(request, "run-real")

    assert response == "```python\nprint('real harness')\n```"
