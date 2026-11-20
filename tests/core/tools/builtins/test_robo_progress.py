from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest

from vibe.core.hardware import HardwareRuntime
from vibe.core.tools.builtins._robo_progress import stream_policy_progress
from vibe.core.types import ToolStreamEvent


@pytest.mark.asyncio
async def test_streams_live_existing_events_then_exact_result(tmp_path):
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    await runtime.connect("sim-arm-1", run_id="run")
    release = asyncio.Event()
    expected = object()

    async def operation():
        await runtime.observe("sim-arm-1", run_id="run")
        await release.wait()
        return expected

    async with aclosing(
        stream_policy_progress(
            operation(), runtime=runtime, run_id="run", tool_call_id="call"
        )
    ) as stream:
        first = await asyncio.wait_for(anext(stream), timeout=2)
        assert isinstance(first, ToolStreamEvent)
        assert "device_observed" in first.message
        assert "device_connected" not in first.message
        assert "points" not in first.message
        release.set()
        remaining = [item async for item in stream]
    assert remaining[-1] is expected
    await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_consumer", [False, True])
async def test_cancel_or_close_joins_owned_operation(tmp_path, close_consumer):
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    await runtime.connect("sim-arm-1", run_id="run")
    stopped = asyncio.Event()

    async def operation():
        try:
            await runtime.observe("sim-arm-1", run_id="run")
            await asyncio.Event().wait()
        finally:
            stopped.set()

    stream = stream_policy_progress(
        operation(), runtime=runtime, run_id="run", tool_call_id="call"
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 2), ToolStreamEvent)
    if close_consumer:
        await stream.aclose()
    else:
        consumer = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    assert stopped.is_set()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_failure_is_not_retried(tmp_path):
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise ValueError("original failure")

    with pytest.raises(ValueError, match="original failure"):
        async for _ in stream_policy_progress(
            operation(), runtime=runtime, run_id="run", tool_call_id="call"
        ):
            pass
    assert calls == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_cancel_real_policy_still_acknowledges_stop(tmp_path):
    from vibe.core.hardware import DeterministicSimulatorAdapter
    from vibe.core.tools.base import InvokeContext
    from vibe.core.tools.builtins.robo import (
        RoboRunPolicy,
        RoboRunPolicyArgs,
        RoboRunPolicyConfig,
    )

    entered = asyncio.Event()

    class SlowAdapter(DeterministicSimulatorAdapter):
        async def execute(self, command):
            entered.set()
            await asyncio.Event().wait()
            return await super().execute(command)

    runtime = HardwareRuntime(adapters=[SlowAdapter()], trace_dir=tmp_path)
    await runtime.connect("sim-arm-1", run_id="run")
    lease = await runtime.acquire_lease("sim-arm-1", owner="test", run_id="run")
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="run")
    tool = RoboRunPolicy.from_config(lambda: RoboRunPolicyConfig(), cwd=tmp_path)
    ctx = InvokeContext(
        tool_call_id="call", hardware_runtime=runtime, session_dir=tmp_path
    )

    async def consume():
        async with aclosing(
            tool.run(
                RoboRunPolicyArgs(
                    device_id="sim-arm-1",
                    lease_id=lease.lease_id,
                    code="home()",
                    run_id="run",
                ),
                ctx,
            )
        ) as stream:
            async for _ in stream:
                pass

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 2)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    kinds = [event.kind for event in runtime.read_trace("run").events]
    assert "stop_acknowledged" in kinds
    assert kinds.count("command_started") == 1
    await runtime.aclose()
