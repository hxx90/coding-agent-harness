from __future__ import annotations

import json

import pytest

from tests.conftest import build_test_agent_loop
from tests.core.agent_loop.test_request_retry import FaultBackend
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent_loop._generation import GenerationControl
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.config.models import GenerationConfig
from vibe.core.types import FunctionCall, LLMMessage, Role, ToolCall


def test_continuous_calls_ignore_evaluation_caps_but_keep_audit(tmp_path):
    control = GenerationControl(
        GenerationConfig(
            enabled=True,
            enforce_limits=False,
            max_calls=1,
            max_seconds=0.001,
            max_input_tokens=1,
            max_output_tokens=1,
            per_call_output_tokens=1,
            audit_dir=str(tmp_path),
        )
    )
    for _ in range(25):
        cap, reservation = control.reserve(
            [LLMMessage(role=Role.user, content="task")], None, 128000
        )
        assert (cap, reservation) == (128000, 0)
        control.finish(
            mock_llm_chunk(prompt_tokens=100, completion_tokens=10), reservation, cap
        )
    assert control.calls == 25
    assert control.deadline is None
    assert control.input_charged == 2500
    assert len((tmp_path / "generation.jsonl").read_text().splitlines()) == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_policy_error_returns_to_model_without_replaying_action(
    build_config, streaming
):
    backend = FakeBackend()
    agent = build_test_agent_loop(
        config=build_config(
            enabled_tools=["robo_run_policy"],
            generation=GenerationConfig(enabled=True, enforce_limits=False),
        ),
        backend=backend,
        agent_name=BuiltinAgentName.AUTO_APPROVE,
        enable_streaming=streaming,
    )
    runtime = agent.hardware_runtime
    await runtime.connect("sim-arm-1", run_id="continuous-error")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="test", run_id="continuous-error"
    )
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="continuous-error")
    backend._streams = [
        [
            mock_llm_chunk(
                tool_calls=[
                    ToolCall(
                        id="first",
                        index=0,
                        function=FunctionCall(
                            name="robo_run_policy",
                            arguments=json.dumps({
                                "device_id": "sim-arm-1",
                                "lease_id": lease.lease_id,
                                "run_id": "continuous-error",
                                "code": "home()\nx = 1 / 0",
                            }),
                        ),
                    )
                ],
                stop_reason="tool_calls",
            )
        ],
        [
            mock_llm_chunk(
                content="I will inspect the state before correcting the failed calculation."
            )
        ],
    ]
    [_ async for _ in agent.act("Perform task")]
    assert len(backend.requests_messages) == 2
    assert any(
        m.role == Role.tool and "policy_runtime" in (m.content or "")
        for m in backend.requests_messages[-1]
    )
    assert runtime._task_actions == 1
    assert runtime._task_action_limit is None


@pytest.mark.asyncio
async def test_continuous_transient_retries_survive_old_retry_and_call_caps(
    build_config, monkeypatch
):
    monkeypatch.setattr(
        "vibe.core.agent_loop._request_retry.retry_delay", lambda *args: 0
    )
    backend = FaultBackend({1, 2, 3, 4})
    agent = build_test_agent_loop(
        config=build_config(
            enabled_tools=["todo"],
            generation=GenerationConfig(
                enabled=True, enforce_limits=False, max_calls=1
            ),
        ),
        backend=backend,
        agent_name=BuiltinAgentName.AUTO_APPROVE,
    )
    [_ async for _ in agent.act("Read todos")]
    assert backend.attempts == 6
    assert agent.generation.calls == 6
