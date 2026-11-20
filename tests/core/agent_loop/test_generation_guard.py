from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from tests.conftest import ConfigBuilder, build_test_agent_loop
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend, FakeInterruptedStreamingBackend
from vibe.core.agent_loop._generation import (
    GenerationBudgetError,
    GenerationControl,
    IncompleteGenerationError,
)
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.config.models import GenerationConfig
from vibe.core.llm.exceptions import IncompleteStreamError
from vibe.core.types import FunctionCall, LLMChunk, LLMMessage, Role, ToolCall


def call_chunk(
    reason: str | None = "tool_calls", args: str = '{"action":"read"}', ident: str = "t"
) -> LLMChunk:
    return mock_llm_chunk(
        content="discard-this-prefix",
        tool_calls=[
            ToolCall(
                id=ident, index=0, function=FunctionCall(name="todo", arguments=args)
            )
        ],
        stop_reason=reason,
    )


def make_agent(
    build_config: ConfigBuilder,
    backend: FakeBackend,
    *,
    streaming: bool = True,
    recover: bool = False,
    max_calls: int = 16,
    limit_tokens: bool = True,
):
    agent = build_test_agent_loop(
        config=build_config(
            enabled_tools=["todo"],
            generation=GenerationConfig(
                enabled=True,
                recover_once=recover,
                max_calls=max_calls,
                limit_tokens=limit_tokens,
            ),
        ),
        backend=backend,
        agent_name=BuiltinAgentName.AUTO_APPROVE,
        enable_streaming=streaming,
    )
    dispatched: list[str] = []
    original = agent._run_tools_concurrently

    async def capture(tool_calls):
        dispatched.extend(call.call_id for call in tool_calls)
        async for event in original(tool_calls):
            yield event

    agent._run_tools_concurrently = capture
    return agent, dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("reason", ["length", "max_tokens", None])
@pytest.mark.parametrize("limit_tokens", [False, True])
async def test_valid_truncated_call_never_dispatches(
    build_config: ConfigBuilder, streaming: bool, reason: str | None, limit_tokens: bool
):
    backend = FakeBackend(call_chunk(reason))
    agent, dispatched = make_agent(
        build_config, backend, streaming=streaming, limit_tokens=limit_tokens
    )
    with pytest.raises((IncompleteGenerationError, IncompleteStreamError)):
        [_ async for _ in agent.act("Read todos")]
    assert dispatched == []
    assert len(backend.requests_messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_provider_default_output_reaches_backend_without_cap(
    build_config: ConfigBuilder, streaming: bool
):
    from vibe.core.llm.backend.base import build_chat_payload

    backend = FakeBackend([
        [call_chunk()],
        [
            mock_llm_chunk(
                content="done", prompt_tokens=2000000, completion_tokens=30000
            )
        ],
    ])
    agent, dispatched = make_agent(
        build_config, backend, streaming=streaming, limit_tokens=False
    )
    [_ async for _ in agent.act("Read todos")]
    assert dispatched == ["t"]
    assert backend.requests_max_tokens == [None, None]
    payload = build_chat_payload(
        model_name="claude-fable-5",
        messages=[],
        temperature=1,
        tools=None,
        max_tokens=backend.requests_max_tokens[-1],
        tool_choice=None,
        thinking="off",
    )
    assert "max_tokens" not in payload
    assert "max_output_tokens" not in payload
    assert agent.generation.output_charged >= 30000


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    ("runtime_cap", "limit_tokens", "expected"),
    [(None, False, 128000), (1024, False, 1024), (None, True, 4096)],
)
async def test_model_output_allowance_reaches_wire(
    build_config: ConfigBuilder,
    streaming: bool,
    runtime_cap: int | None,
    limit_tokens: bool,
    expected: int,
):
    import json

    from vibe.core.llm.backend.generic import OpenAIAdapter

    backend = FakeBackend(mock_llm_chunk(content="done"))
    agent, _ = make_agent(
        build_config, backend, streaming=streaming, limit_tokens=limit_tokens
    )
    model = agent.config.get_active_model()
    model.max_output_tokens = 128000
    if runtime_cap is not None:
        agent.set_max_tokens(runtime_cap)
    [_ async for _ in agent.act("Say done")]
    assert backend.requests_max_tokens == [expected]
    request = OpenAIAdapter().prepare_request(
        model_name=model.name,
        messages=backend.requests_messages[0],
        temperature=model.temperature,
        tools=None,
        max_tokens=backend.requests_max_tokens[0],
        tool_choice=None,
        enable_streaming=streaming,
        provider=agent.config.get_active_provider(),
    )
    assert json.loads(request.body)["max_tokens"] == expected


def test_no_token_limits_preserves_audit_and_request_deadline(tmp_path):
    import json

    control = GenerationControl(
        GenerationConfig(
            enabled=True,
            limit_tokens=False,
            max_calls=2,
            max_input_tokens=1,
            max_output_tokens=1,
            per_call_output_tokens=1,
            audit_dir=str(tmp_path),
        )
    )
    messages = [LLMMessage(role=Role.user, content="larger than token quota")]
    cap, reserved = control.reserve(messages, None, None)
    assert (cap, reserved) == (None, 0)
    control.finish(None, reserved, cap, "interrupted")
    control.reserve(messages, None, None)
    with pytest.raises(GenerationBudgetError):
        control.reserve(messages, None, None)
    events = [
        json.loads(line)
        for line in (tmp_path / "generation.jsonl").read_text().splitlines()
    ]
    assert events[1]["usage_missing"] is True
    control.calls = 0
    control.deadline = 0
    with pytest.raises(GenerationBudgetError):
        control.reserve(messages, None, None)


@pytest.mark.asyncio
async def test_normal_complete_call_executes(build_config: ConfigBuilder):
    agent, dispatched = make_agent(
        build_config, FakeBackend([[call_chunk()], [mock_llm_chunk(content="done")]])
    )
    [_ async for _ in agent.act("Read todos")]
    assert dispatched == ["t"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args", ['{"action":', '{"action":"read"} trailing', "null", ""]
)
async def test_incomplete_args_reject_whole_batch(
    build_config: ConfigBuilder, args: str
):
    bad = call_chunk(args=args)
    good = call_chunk(ident="good")
    bad = bad.model_copy(
        update={
            "message": bad.message.model_copy(
                update={
                    "tool_calls": [
                        *(good.message.tool_calls or []),
                        *(bad.message.tool_calls or []),
                    ]
                }
            )
        }
    )
    agent, dispatched = make_agent(build_config, FakeBackend(bad))
    with pytest.raises(IncompleteGenerationError):
        [_ async for _ in agent.act("Read todos")]
    assert dispatched == []


@pytest.mark.asyncio
async def test_fresh_recovery_never_replays_prior_execution(
    build_config: ConfigBuilder,
):
    backend = FakeBackend([
        [call_chunk(ident="executed")],
        [call_chunk("length", ident="rejected")],
        [call_chunk(ident="fresh")],
        [mock_llm_chunk(content="done")],
    ])
    agent, dispatched = make_agent(build_config, backend, recover=True)
    [_ async for _ in agent.act("Read todos")]
    assert dispatched == ["executed", "fresh"]
    assert agent.generation.recovery_used
    retry_messages = backend.requests_messages[2]
    assert not any(
        c.id == "rejected" for m in retry_messages for c in m.tool_calls or []
    )
    assert any(m.role == Role.tool for m in retry_messages)
    assert any(
        "Current device observations" in (m.content or "") for m in retry_messages
    )


@pytest.mark.asyncio
async def test_second_truncation_is_terminal(build_config: ConfigBuilder):
    backend = FakeBackend([[call_chunk("length")], [call_chunk("length")]])
    agent, dispatched = make_agent(build_config, backend, recover=True)
    with pytest.raises(IncompleteGenerationError):
        [_ async for _ in agent.act("Read todos")]
    assert dispatched == []
    assert len(backend.requests_messages) == 2


@pytest.mark.asyncio
async def test_recovery_shares_request_budget(build_config: ConfigBuilder):
    backend = FakeBackend(call_chunk("length"))
    agent, dispatched = make_agent(build_config, backend, recover=True, max_calls=1)
    with pytest.raises(GenerationBudgetError):
        [_ async for _ in agent.act("Read todos")]
    assert dispatched == []
    assert len(backend.requests_messages) == 1


@pytest.mark.asyncio
async def test_stream_interruption_discards_prefix(build_config: ConfigBuilder):
    backend = FakeInterruptedStreamingBackend([
        [call_chunk(ident="partial")],
        [call_chunk(ident="fresh")],
        [mock_llm_chunk(content="done")],
    ])
    agent, dispatched = make_agent(build_config, backend, recover=True)
    [_ async for _ in agent.act("Read todos")]
    assert dispatched == ["fresh"]
    assert not any(
        m.content == "discard-this-prefix" for m in backend.requests_messages[1]
    )


class CancelBackend(FakeBackend):
    async def complete_streaming(self, **kwargs: Any) -> AsyncGenerator[LLMChunk]:
        async for chunk in super().complete_streaming(**kwargs):
            yield chunk
            raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_cancel_never_regenerates(build_config: ConfigBuilder):
    backend = CancelBackend(call_chunk())
    agent, dispatched = make_agent(build_config, backend, recover=True)
    with pytest.raises(asyncio.CancelledError):
        [_ async for _ in agent.act("Read todos")]
    assert dispatched == []
    assert not agent.generation.recovery_used
    assert agent.generation.output_charged == 4096


@pytest.mark.asyncio
async def test_execution_error_is_outside_regeneration(build_config: ConfigBuilder):
    backend = FakeBackend(call_chunk())
    agent, dispatched = make_agent(build_config, backend, recover=True)

    async def broken(tool_calls):
        dispatched.extend(c.call_id for c in tool_calls)
        raise RuntimeError("action already executed; statement_limit")
        yield

    agent._run_tools_concurrently = broken
    with pytest.raises(RuntimeError, match="already executed"):
        [_ async for _ in agent.act("Read todos")]
    assert dispatched == ["t"]
    assert len(backend.requests_messages) == 1
    assert not agent.generation.recovery_used


def test_token_reservations_and_unknown_usage_are_not_free():
    control = GenerationControl(
        GenerationConfig(enabled=True, max_output_tokens=6, per_call_output_tokens=4)
    )
    messages = [LLMMessage(role=Role.user, content="x")]
    cap, reservation = control.reserve(messages, None, None)
    assert cap == 4
    control.finish(None, reservation, cap, "interrupted")
    cap, _ = control.reserve(messages, None, None)
    assert cap == 2
    with pytest.raises(GenerationBudgetError):
        control.reserve(messages, None, None)
    tiny = GenerationControl(GenerationConfig(enabled=True, max_input_tokens=1))
    with pytest.raises(GenerationBudgetError):
        tiny.reserve(messages, None, None)
    assert tiny.calls == 0


@pytest.mark.asyncio
async def test_task_deadline_cancels_without_regeneration(build_config: ConfigBuilder):
    class SlowBackend(FakeBackend):
        async def complete_streaming(self, **kwargs: Any) -> AsyncGenerator[LLMChunk]:
            await asyncio.sleep(1)
            async for chunk in super().complete_streaming(**kwargs):
                yield chunk

    agent = build_test_agent_loop(
        config=build_config(
            generation=GenerationConfig(
                enabled=True, recover_once=True, max_seconds=0.3
            )
        ),
        backend=SlowBackend(call_chunk()),
        enable_streaming=True,
    )
    with pytest.raises(TimeoutError):
        [_ async for _ in agent.act("Read todos")]
    assert not agent.generation.recovery_used
    assert agent.generation.output_charged == 4096


def test_existing_rgb_input_is_preserved_and_reserved(tmp_path):
    from PIL import Image

    from vibe.core.types import FileImageSource, ImageAttachment

    path = tmp_path / "rgb.png"
    Image.new("RGB", (384, 384)).save(path)
    attachment = ImageAttachment(
        source=FileImageSource(path=path), alias="rgb", mime_type="image/png"
    )
    messages = [LLMMessage(role=Role.user, content="look", images=[attachment])]
    control = GenerationControl(GenerationConfig(enabled=True))
    _, reserved = control.reserve(messages, None, None)
    assert reserved > 384 * 384
    assert messages[0].images == [attachment]


@pytest.mark.asyncio
async def test_actions_exhaust_independently_of_cleanup():
    from vibe.core.hardware import (
        DeterministicSimulatorAdapter,
        HardwareCommand,
        HardwareRuntime,
        HardwareRuntimeError,
    )

    runtime = HardwareRuntime(
        adapters=[DeterministicSimulatorAdapter()], task_action_limit=1
    )
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="test")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="test", ttl_seconds=60, run_id="test"
    )
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="test")
    command = HardwareCommand(
        command_id="first",
        device_id="sim-arm-1",
        lease_id=lease.lease_id,
        action="home",
        parameters={"object": "cube"},
    )
    await runtime.execute(command, run_id="test")
    with pytest.raises(HardwareRuntimeError, match="action budget"):
        await runtime.execute(
            command.model_copy(update={"command_id": "second"}), run_id="test"
        )
    cleanup = await runtime.bounded_cleanup()
    assert cleanup["actions"] == 1
    assert runtime.read_trace("bounded-cleanup").events[-1].kind == "lease_released"
    await runtime.aclose()


def test_provider_overage_is_recorded_and_fails_closed(tmp_path):
    import json

    control = GenerationControl(GenerationConfig(enabled=True, audit_dir=str(tmp_path)))
    cap, reserved = control.reserve(
        [LLMMessage(role=Role.user, content="x")], None, None
    )
    chunk = mock_llm_chunk(prompt_tokens=reserved + 1)
    with pytest.raises(GenerationBudgetError, match="Provider exceeded"):
        control.finish(chunk, reserved, cap)
    events = [
        json.loads(line)
        for line in (tmp_path / "generation.jsonl").read_text().splitlines()
    ]
    assert events[-2]["response"]["usage"]["prompt_tokens"] == reserved + 1
    assert events[-1]["kind"] == "provider_budget_violation"
