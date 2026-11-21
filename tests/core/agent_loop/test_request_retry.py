from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tests.core.agent_loop.test_generation_guard import call_chunk, make_agent
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent_loop._generation import GenerationBudgetError
from vibe.core.llm.exceptions import BackendError, PayloadSummary


def gateway_error(
    status=403,
    *,
    code="get_channel_failed",
    message="upstream StatusCode: 429",
    host="api-gateway.glm.ai",
    headers=None,
):
    return BackendError(
        provider="gateway",
        endpoint=f"https://{host}/v1/chat/completions",
        status=status,
        reason="test",
        headers=headers,
        body_text=json.dumps({"error": {"code": code, "message": message}}),
        parsed_error=message,
        model="test",
        payload_summary=PayloadSummary(
            model="test",
            message_count=1,
            approx_chars=1,
            temperature=1,
            has_tools=True,
            tool_choice=None,
        ),
    )


class FaultBackend(FakeBackend):
    def __init__(self, fail_on, *, error=None, partial=False):
        super().__init__([[call_chunk()], [mock_llm_chunk(content="done")]])
        self.fail_on = fail_on
        self.error = error or gateway_error()
        self.partial = partial
        self.attempts = 0

    async def complete(self, **kwargs: Any):
        self.attempts += 1
        if self.attempts in self.fail_on:
            raise self.error
        return await super().complete(**kwargs)

    async def complete_streaming(self, **kwargs: Any):
        self.attempts += 1
        if self.attempts in self.fail_on:
            if self.partial:
                yield call_chunk()  # syntactically valid tool prefix must not execute
            raise self.error
        async for chunk in super().complete_streaming(**kwargs):
            yield chunk


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_transient_gateway_retries_before_dispatch(build_config, streaming):
    backend = FaultBackend({1})
    agent, dispatched = make_agent(
        build_config, backend, streaming=streaming, limit_tokens=False
    )
    [_ async for _ in agent.act("Read todos")]
    assert backend.attempts == agent.generation.calls == 3
    assert dispatched == ["t"]


@pytest.mark.asyncio
async def test_gateway_failure_after_tool_does_not_replay_action(build_config):
    backend = FaultBackend({2})
    agent, dispatched = make_agent(build_config, backend, limit_tokens=False)
    [_ async for _ in agent.act("Read todos")]
    assert dispatched == ["t"]
    assert backend.attempts == agent.generation.calls == 3


@pytest.mark.asyncio
async def test_gateway_retry_limit_and_shared_request_budget(build_config):
    backend = FaultBackend(set(range(20)))
    agent, dispatched = make_agent(build_config, backend, limit_tokens=False)
    with pytest.raises(RuntimeError):
        [_ async for _ in agent.act("Read todos")]
    assert backend.attempts == agent.generation.calls == 3
    assert not dispatched
    backend = FaultBackend(set(range(20)))
    agent, dispatched = make_agent(
        build_config, backend, limit_tokens=False, max_calls=1
    )
    with pytest.raises(GenerationBudgetError):
        [_ async for _ in agent.act("Read todos")]
    assert backend.attempts == agent.generation.calls == 1
    assert not dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        gateway_error(401),
        gateway_error(403, code="permission_denied"),
        gateway_error(403, host="other.example"),
        gateway_error(403, message="API quota exhausted; IP not allowed"),
        gateway_error(400),
    ],
)
async def test_permanent_errors_are_not_retried(build_config, error):
    backend = FaultBackend({1}, error=error)
    agent, dispatched = make_agent(build_config, backend, limit_tokens=False)
    with pytest.raises(RuntimeError):
        [_ async for _ in agent.act("Read todos")]
    assert backend.attempts == 1
    assert not dispatched


@pytest.mark.asyncio
async def test_partial_stream_is_not_retried_or_dispatched(build_config):
    backend = FaultBackend({1}, partial=True)
    agent, dispatched = make_agent(build_config, backend, limit_tokens=False)
    with pytest.raises(RuntimeError):
        [_ async for _ in agent.act("Read todos")]
    assert backend.attempts == 1
    assert not dispatched


@pytest.mark.asyncio
async def test_cancel_during_backoff_stops_retry(build_config):
    backend = FaultBackend({1})
    agent, dispatched = make_agent(build_config, backend, limit_tokens=False)
    entered = asyncio.Event()

    async def notice(reason):
        entered.set()

    agent.notice_retry = notice

    async def run():
        return [_ async for _ in agent.act("Read todos")]

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.attempts == 1
        assert not dispatched
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503, None])
async def test_bounded_standard_transient_errors_are_accounted(build_config, status):
    error = gateway_error(status, message="Network error" if status is None else "busy")
    backend = FaultBackend({1}, error=error)
    agent, dispatched = make_agent(build_config, backend, limit_tokens=False)
    [_ async for _ in agent.act("Read todos")]
    assert backend.attempts == agent.generation.calls == 3
    assert dispatched == ["t"]


@pytest.mark.asyncio
async def test_retry_does_not_wait_past_shared_deadline(build_config):
    import time

    backend = FaultBackend({1})
    agent, dispatched = make_agent(build_config, backend, limit_tokens=False)
    agent.generation.deadline = time.monotonic() + 1
    with pytest.raises(GenerationBudgetError):
        [_ async for _ in agent.act("Read todos")]
    assert backend.attempts == 1
    assert not dispatched


@pytest.mark.asyncio
async def test_long_retry_after_is_not_ignored(build_config):
    backend = FaultBackend(
        {1}, error=gateway_error(429, headers={"Retry-After": "120"})
    )
    agent, dispatched = make_agent(build_config, backend, limit_tokens=False)
    with pytest.raises(RuntimeError):
        [_ async for _ in agent.act("Read todos")]
    assert backend.attempts == 1
    assert not dispatched


def test_retry_after_delay_and_legacy_backend_ownership():
    from datetime import UTC, datetime, timedelta
    import email.utils

    from vibe.core.agent_loop._request_retry import request_retry_reason, retry_delay

    assert retry_delay(gateway_error(429, headers={"Retry-After": "12"}), 1) == 12
    date = email.utils.format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    assert 28 <= retry_delay(gateway_error(429, headers={"Retry-After": date}), 1) <= 30
    assert request_retry_reason(gateway_error(429), bounded=False) is None
    assert request_retry_reason(gateway_error(403), bounded=False) is not None
