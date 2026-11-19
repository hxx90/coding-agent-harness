from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vibe.core.hardware import (
    HardwarePolicyError,
    HardwarePolicyRunner,
    HardwareRuntime,
)


@pytest.mark.asyncio
async def test_policy_runs_code_against_manifest_actions_and_verifies_result(
    tmp_path: Path,
) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path / "traces")
    await runtime.connect("sim-arm-1", run_id="policy-fold")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="policy-test", run_id="policy-fold"
    )
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="policy-fold")
    runner = HardwarePolicyRunner(runtime, artifact_dir=tmp_path / "policies")

    result = await runner.run(
        """
before = observe()
fold_cloth(cloth="shirt")
after = observe()
report = verify("cloth_folded", cloth="shirt")
print(report["status"])
""".strip(),
        device_id="sim-arm-1",
        lease_id=lease.lease_id,
        run_id="policy-fold",
    )

    assert result.task_outcome == "passed"
    assert [receipt.status for receipt in result.receipts] == ["completed"]
    assert result.snapshot.state["folded_cloths"] == ["shirt"]
    assert result.report is not None
    assert result.report.status == "passed"
    assert result.stdout == ["passed"]
    assert [step.kind for step in result.steps] == [
        "observation",
        "action",
        "observation",
        "verification",
        "output",
    ]
    assert result.source_uri.startswith("file://")
    assert (tmp_path / "policies" / f"{result.policy_id}.py").read_text() == (
        result.source
    )
    await runtime.aclose()


@pytest.mark.asyncio
async def test_policy_validates_the_whole_program_before_moving_hardware(
    tmp_path: Path,
) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path / "traces")
    await runtime.connect("sim-arm-1", run_id="policy-static-rejection")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="policy-test", run_id="policy-static-rejection"
    )
    await runtime.arm(
        "sim-arm-1", lease_id=lease.lease_id, run_id="policy-static-rejection"
    )
    runner = HardwarePolicyRunner(runtime, artifact_dir=tmp_path / "policies")

    with pytest.raises(HardwarePolicyError) as raised:
        await runner.run(
            'fold_cloth(cloth="shirt")\nimport os',
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            run_id="policy-static-rejection",
        )

    snapshot = await runtime.observe("sim-arm-1", run_id="policy-static-rejection")
    assert raised.value.code == "unsafe_policy"
    assert raised.value.line == 2
    assert raised.value.source_uri is not None
    assert snapshot.status == "armed"
    assert snapshot.state["folded_cloths"] == []
    await runtime.aclose()


@pytest.mark.asyncio
async def test_policy_stops_hardware_when_a_runtime_limit_is_exceeded(
    tmp_path: Path,
) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path / "traces")
    await runtime.connect("sim-arm-1", run_id="policy-loop-limit")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="policy-test", run_id="policy-loop-limit"
    )
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="policy-loop-limit")
    runner = HardwarePolicyRunner(
        runtime, artifact_dir=tmp_path / "policies", max_loop_iterations=2
    )

    with pytest.raises(HardwarePolicyError) as raised:
        await runner.run(
            "while True:\n    observe()",
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            run_id="policy-loop-limit",
        )

    snapshot = await runtime.observe("sim-arm-1", run_id="policy-loop-limit")
    assert raised.value.code == "loop_limit"
    assert raised.value.line == 1
    assert raised.value.source_uri is not None
    assert snapshot.status == "stopped"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_policy_reports_runtime_source_location_and_stops_hardware(
    tmp_path: Path,
) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path / "traces")
    await runtime.connect("sim-arm-1", run_id="policy-runtime-error")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="policy-test", run_id="policy-runtime-error"
    )
    await runtime.arm(
        "sim-arm-1", lease_id=lease.lease_id, run_id="policy-runtime-error"
    )
    runner = HardwarePolicyRunner(runtime, artifact_dir=tmp_path / "policies")

    with pytest.raises(HardwarePolicyError) as raised:
        await runner.run(
            'before = observe()\nmissing = before["missing"]',
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            run_id="policy-runtime-error",
        )

    snapshot = await runtime.observe("sim-arm-1", run_id="policy-runtime-error")
    assert raised.value.code == "policy_runtime"
    assert raised.value.line == 2
    assert raised.value.column == 1
    assert raised.value.source_uri is not None
    assert snapshot.status == "stopped"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_policy_timeout_requests_an_acknowledged_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path / "traces")
    await runtime.connect("sim-arm-1", run_id="policy-timeout")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="policy-test", run_id="policy-timeout"
    )
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="policy-timeout")
    original_observe = runtime.observe

    async def slow_observe(device_id: str, *, run_id: str):
        await asyncio.sleep(60)
        return await original_observe(device_id, run_id=run_id)

    monkeypatch.setattr(runtime, "observe", slow_observe)
    runner = HardwarePolicyRunner(
        runtime, artifact_dir=tmp_path / "policies", timeout_seconds=0.01
    )

    with pytest.raises(HardwarePolicyError) as raised:
        await runner.run(
            "observe()",
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            run_id="policy-timeout",
        )

    event_kinds = [event.kind for event in runtime.read_trace("policy-timeout").events]
    assert raised.value.code == "policy_timeout"
    assert raised.value.source_uri is not None
    assert "stop_requested" in event_kinds
    assert "stop_acknowledged" in event_kinds
    await runtime.aclose()


@pytest.mark.asyncio
async def test_policy_cancellation_requests_an_acknowledged_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path / "traces")
    await runtime.connect("sim-arm-1", run_id="policy-cancelled")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="policy-test", run_id="policy-cancelled"
    )
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="policy-cancelled")
    observation_started = asyncio.Event()

    async def blocked_observe(device_id: str, *, run_id: str):
        del device_id, run_id
        observation_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "observe", blocked_observe)
    runner = HardwarePolicyRunner(runtime, artifact_dir=tmp_path / "policies")
    task = asyncio.create_task(
        runner.run(
            "observe()",
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            run_id="policy-cancelled",
        )
    )
    await asyncio.wait_for(observation_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    event_kinds = [
        event.kind for event in runtime.read_trace("policy-cancelled").events
    ]
    assert "stop_requested" in event_kinds
    assert "stop_acknowledged" in event_kinds
    await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        'verify("holding_object", object="cube")',
        'home()\nverify("holding_object", object="cube")',
        'fold_cloth(cloth="shirt")\nverify("cloth_folded", cloth="shirt")\nhome()',
    ],
)
async def test_failed_or_outdated_verification_never_reports_success(tmp_path, source):
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    try:
        await runtime.connect("sim-arm-1", run_id="no-false-success")
        lease = await runtime.acquire_lease(
            "sim-arm-1", owner="test", run_id="no-false-success"
        )
        await runtime.arm(
            "sim-arm-1", lease_id=lease.lease_id, run_id="no-false-success"
        )
        result = await HardwarePolicyRunner(
            runtime, artifact_dir=tmp_path / "policies"
        ).run(
            source,
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            run_id="no-false-success",
        )
        assert result.task_outcome != "passed"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_result_persistence_failure_stops_device(tmp_path, monkeypatch):
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    original_write = Path.write_text

    def fail_result(path, *args, **kwargs):
        if path.suffix == ".json":
            raise OSError("test disk full")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_result)
    try:
        await runtime.connect("sim-arm-1", run_id="disk-full")
        lease = await runtime.acquire_lease(
            "sim-arm-1", owner="test", run_id="disk-full"
        )
        await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="disk-full")
        with pytest.raises(HardwarePolicyError) as raised:
            await HardwarePolicyRunner(runtime, artifact_dir=tmp_path / "policies").run(
                "home()",
                device_id="sim-arm-1",
                lease_id=lease.lease_id,
                run_id="disk-full",
            )
        assert raised.value.code == "artifact_write_failed"
        assert (
            await runtime.observe("sim-arm-1", run_id="disk-full")
        ).status == "stopped"
    finally:
        await runtime.aclose()
