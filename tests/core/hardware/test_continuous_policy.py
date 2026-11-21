from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from vibe.core.hardware import HardwarePolicyRunner, HardwareRuntime


@pytest.mark.asyncio
async def test_default_policy_runs_past_old_limits_with_complete_disk_trace(tmp_path):
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    try:
        await runtime.connect("sim-arm-1", run_id="continuous")
        lease = await runtime.acquire_lease(
            "sim-arm-1", owner="test", run_id="continuous"
        )
        await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="continuous")
        result = await HardwarePolicyRunner(runtime, artifact_dir=tmp_path).run(
            "total = 0\nfor i in range(250):\n    total += i\n    print(total)\nfor i in range(75):\n    home()\nprint(total)",
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            run_id="continuous",
        )
        assert result.statement_count > 200
        assert result.loop_iterations == 325
        assert result.action_count == 75
        assert result.stdout[-1] == str(sum(range(250)))
        assert len(result.stdout) == 100
        assert len(result.receipts) == 50
        assert len(result.steps) == 200
        assert result.history_truncated
        assert result.trace_uri is not None
        full = [
            json.loads(line)
            for line in Path(result.trace_uri.removeprefix("file://"))
            .read_text()
            .splitlines()
        ]
        assert len(full) == 326
        assert [step["sequence"] for step in full] == list(range(1, 327))
        assert sum(step["kind"] == "action" for step in full) == 75
        assert result.task_outcome == "not_verified"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_pure_infinite_computation_is_cancellable_and_stops(tmp_path):
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    try:
        await runtime.connect("sim-arm-1", run_id="cancel-compute")
        lease = await runtime.acquire_lease(
            "sim-arm-1", owner="test", run_id="cancel-compute"
        )
        await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="cancel-compute")
        task = asyncio.create_task(
            HardwarePolicyRunner(runtime, artifact_dir=tmp_path).run(
                "while True:\n    pass",
                device_id="sim-arm-1",
                lease_id=lease.lease_id,
                run_id="cancel-compute",
            )
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert "stop_acknowledged" in [
            e.kind for e in runtime.read_trace("cancel-compute").events
        ]
    finally:
        await runtime.aclose()
