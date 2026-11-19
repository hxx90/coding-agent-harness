from __future__ import annotations

import asyncio
import json
from pathlib import Path
import platform
import re
import sys
from typing import Any

from pydantic import ValidationError
import pytest

from vibe.core.hardware import (
    HardwareCommand,
    HardwarePolicyError,
    HardwarePolicyRunner,
    HardwareRuntime,
)
from vibe.hardware_sim._target_motion import run_target_motion
from vibe.hardware_sim.adapter import RobosuiteSimulatorAdapter
from vibe.hardware_sim.models import TargetMoveParameters, TargetMoveReport


@pytest.mark.parametrize(
    "parameters",
    [
        {"target_xyz": [float("nan"), 0, 1]},
        {"target_xyz": [0, 0, 1], "max_steps": 401},
        {"target_xyz": [0, 0, 1], "max_steps": True},
        {"target_xyz": [0, 0, 1], "tolerance_xyz": [0.1, 0.1, 0.1]},
        {"target_xyz": [0, 0, 1], "timeout_seconds": 121},
        {"target_xyz": [0, 0, 1], "unknown": 1},
    ],
)
def test_target_move_rejects_unbounded_parameters(parameters: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TargetMoveParameters.model_validate(parameters)


@pytest.mark.asyncio
async def test_timeout_counts_completed_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = 0.0
    cycles = 0
    reports: list[TargetMoveReport] = []

    async def advance(error: tuple[float, float, float]) -> None:
        nonlocal clock, cycles
        cycles += 1
        clock = 2.0

    monkeypatch.setattr(
        "vibe.hardware_sim._target_motion.time.monotonic", lambda: clock
    )
    result = await run_target_motion(
        TargetMoveParameters(target_xyz=(0, 0, 1), timeout_seconds=1),
        position=lambda: (0, 0, 1),
        advance=advance,
        cycles_used=lambda: cycles,
        cycles_remaining=lambda: 72 - cycles,
        stopped=lambda: False,
        finish=reports.append,
    )
    assert result.reason == "time_limit"
    assert result.cycles == 1
    assert not result.reached
    assert reports == [result]


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_and_counts_inflight_cycle() -> None:
    entered = asyncio.Event()
    blocked = asyncio.Event()
    cycles = 0
    reports: list[TargetMoveReport] = []

    async def advance(error: tuple[float, float, float]) -> None:
        nonlocal cycles
        cycles += 1
        entered.set()
        await blocked.wait()

    task = asyncio.create_task(
        run_target_motion(
            TargetMoveParameters(target_xyz=(0, 0, 1)),
            position=lambda: (0, 0, 0),
            advance=advance,
            cycles_used=lambda: cycles,
            cycles_remaining=lambda: 72 - cycles,
            stopped=lambda: False,
            finish=reports.append,
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reports[0].reason == "cancelled"
    assert reports[0].cycles == 1


@pytest.mark.asyncio
async def test_legacy_delta_repeats_and_shares_cycle_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    np = pytest.importorskip("numpy")
    adapter = RobosuiteSimulatorAdapter(show_viewer=False, max_control_cycles=2)
    applied: list[list[float]] = []

    class FakeEnvironment:
        def step(
            self, action: Any
        ) -> tuple[dict[str, object], int, bool, dict[str, object]]:
            applied.append(action.tolist())
            return {"robot0_eef_pos": [0, 0, 1]}, 0, False, {}

    adapter._environment = FakeEnvironment()
    monkeypatch.setattr(adapter, "_zero_action", lambda: np.zeros(7))
    monkeypatch.setattr(adapter, "_render", lambda: None)
    monkeypatch.setattr(adapter, "_state", lambda mode: {})
    await adapter._apply_action(
        "move_cartesian", {"delta_xyz": [0.2, 0, 0], "steps": 2}
    )
    assert applied == [[0.2, 0, 0, 0, 0, 0, 0]] * 2
    report = await adapter._move_to(TargetMoveParameters(target_xyz=(0, 0, 1)))
    assert report.reason == "cycle_budget"
    assert report.cycles == 0
    assert len(applied) == 2
    assert not adapter._armed


async def _armed_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[HardwareRuntime, RobosuiteSimulatorAdapter, str]:
    monkeypatch.setattr(
        sys,
        "platform",
        {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}[platform.system()],
    )
    pytest.importorskip("robosuite")
    adapter = RobosuiteSimulatorAdapter(
        show_viewer=False,
        observation_mode="rgbd",
        geometry_library="off",
        seed=821,
        evidence_dir=tmp_path / "evidence",
        max_control_cycles=92,
    )
    runtime = HardwareRuntime(
        adapters=[adapter], trace_dir=tmp_path, task_action_limit=80
    )
    await runtime.connect("mujoco-panda-1", run_id="target")
    lease = await runtime.acquire_lease("mujoco-panda-1", owner="test", run_id="target")
    await runtime.arm("mujoco-panda-1", lease_id=lease.lease_id, run_id="target")
    return runtime, adapter, lease.lease_id


@pytest.mark.asyncio
@pytest.mark.parametrize("use_act", [False, True])
async def test_documented_receipt_example_runs_with_full_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_act: bool
) -> None:
    # Execute the published example through the real policy/runtime/adapter seam.
    # Changing the prose without keeping its return path executable must fail here.
    prompt = (
        Path(__file__).parents[2]
        / "vibe/core/tools/builtins/prompts/robo_run_policy.md"
    ).read_text()
    examples = re.findall(r"```python\n(.*?)\n```", prompt, re.DOTALL)
    source = next(code for code in examples if "motion = receipt[" in code)
    if use_act:
        source = source.replace("move_to(target_xyz=", 'act("move_to", target_xyz=')
    runtime, adapter, lease = await _armed_runtime(tmp_path, monkeypatch)
    try:
        manifest = (await adapter.discover())[0]
        description = next(
            a.description for a in manifest.actions if a.name == "move_to"
        )
        assert "receipt['result']['motion']['reached']" in description
        runner = HardwarePolicyRunner(runtime, artifact_dir=tmp_path / "policies")
        result = await runner.run(
            source, device_id="mujoco-panda-1", lease_id=lease, run_id="receipt-example"
        )
        assert result.stdout[0].startswith("true reached ")
        receipt = result.receipts[0].model_dump(mode="json")
        assert receipt["status"] == "completed"
        assert receipt["result"]["motion"]["reached"] is True
        assert "motion" not in receipt
        assert len(result.receipts) == 1
        assert result.report is None  # Holding position is not a task verification.
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "reason", "cycles"),
    [
        ("max_steps=1", "step_limit", 1),
        ("max_steps=72, timeout_seconds=1e-9", "time_limit", 0),
    ],
)
async def test_failed_target_aborts_policy_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: str,
    reason: str,
    cycles: int,
) -> None:
    runtime, adapter, lease = await _armed_runtime(tmp_path, monkeypatch)
    try:
        runner = HardwarePolicyRunner(runtime, artifact_dir=tmp_path / "policies")
        with pytest.raises(HardwarePolicyError, match=reason):
            await runner.run(
                f"move_to(target_xyz=[0, 0, 0.9], {options})\ngripper_close()",
                device_id="mujoco-panda-1",
                lease_id=lease,
                run_id="target",
            )
        saved = json.loads(next((tmp_path / "policies").glob("*.json")).read_text())
        actions = [s for s in saved["steps"] if s["kind"] == "action"]
        assert [s["name"] for s in actions] == ["move_to"]
        assert actions[0]["output"]["result"]["motion"]["cycles"] == cycles
        assert actions[0]["output"]["result"]["motion"]["reached"] is False
        assert adapter._stopped
        assert adapter._control_cycles_used == cycles
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_operator_stop_interrupts_target_and_preserves_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, adapter, lease = await _armed_runtime(tmp_path, monkeypatch)
    first_cycle = asyncio.Event()
    original = adapter._render

    def rendered() -> None:
        original()
        first_cycle.set()

    monkeypatch.setattr(adapter, "_render", rendered)
    task = asyncio.create_task(
        runtime.execute(
            HardwareCommand(
                device_id="mujoco-panda-1",
                lease_id=lease,
                action="move_to",
                parameters={"target_xyz": [0, 0, 0.9], "max_steps": 72},
            ),
            run_id="target",
        )
    )
    try:
        await first_cycle.wait()
        ack = await runtime.stop(
            "mujoco-panda-1", reason="test cancellation", run_id="target"
        )
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ack.acknowledged
        report = adapter._last_target_move
        assert report is not None
        assert report.reason == "cancelled"
        assert 0 < report.cycles < 72
        assert adapter._control_cycles_used == report.cycles
        assert report.trace_uri is not None
        assert (
            len(Path(report.trace_uri.removeprefix("file://")).read_text().splitlines())
            == report.cycles
        )
        await asyncio.sleep(0)
        assert adapter._control_cycles_used == report.cycles
        await runtime.release_lease("mujoco-panda-1", lease_id=lease, run_id="target")
    finally:
        await runtime.aclose()
