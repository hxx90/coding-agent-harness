from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import platform
import sys
import time

import pytest

from vibe.core.hardware import HardwareCommand, HardwareRuntime
from vibe.hardware_sim import MUJOCO_PANDA_DEVICE_ID, RobosuiteSimulatorAdapter

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("robosuite") is None, reason="requires the simulator extra"
)


@pytest.mark.asyncio
async def test_runtime_controls_headless_mujoco_panda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_platform = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
    monkeypatch.setattr(sys, "platform", system_platform[platform.system()])
    adapter = RobosuiteSimulatorAdapter(show_viewer=False)
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)

    manifests = await runtime.discover()
    await runtime.connect(MUJOCO_PANDA_DEVICE_ID, run_id="mujoco-smoke")
    lease = await runtime.acquire_lease(
        MUJOCO_PANDA_DEVICE_ID, owner="test", run_id="mujoco-smoke"
    )
    await runtime.arm(
        MUJOCO_PANDA_DEVICE_ID, lease_id=lease.lease_id, run_id="mujoco-smoke"
    )
    before = await runtime.observe(MUJOCO_PANDA_DEVICE_ID, run_id="mujoco-smoke")
    receipt = await runtime.execute(
        HardwareCommand(
            device_id=MUJOCO_PANDA_DEVICE_ID,
            lease_id=lease.lease_id,
            action="move_cartesian",
            parameters={"delta_xyz": [0.2, 0.0, 0.0], "steps": 10},
        ),
        run_id="mujoco-smoke",
    )
    after = await runtime.observe(MUJOCO_PANDA_DEVICE_ID, run_id="mujoco-smoke")
    stopped = await runtime.stop(
        MUJOCO_PANDA_DEVICE_ID, reason="test complete", run_id="mujoco-smoke"
    )

    assert [manifest.device_id for manifest in manifests] == [MUJOCO_PANDA_DEVICE_ID]
    assert receipt.status == "completed"
    assert after.state["end_effector_position"] != before.state["end_effector_position"]
    assert stopped.acknowledged is True

    await runtime.aclose()


@pytest.mark.asyncio
async def test_stop_interrupts_long_mujoco_motion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_platform = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
    monkeypatch.setattr(sys, "platform", system_platform[platform.system()])
    adapter = RobosuiteSimulatorAdapter(show_viewer=False)
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.connect(MUJOCO_PANDA_DEVICE_ID, run_id="mujoco-stop")
    lease = await runtime.acquire_lease(
        MUJOCO_PANDA_DEVICE_ID, owner="test", run_id="mujoco-stop"
    )
    await runtime.arm(
        MUJOCO_PANDA_DEVICE_ID, lease_id=lease.lease_id, run_id="mujoco-stop"
    )

    first_step = asyncio.Event()
    original_render = adapter._render

    def slow_render() -> None:
        original_render()
        first_step.set()
        time.sleep(0.01)

    monkeypatch.setattr(adapter, "_render", slow_render)
    execution = asyncio.create_task(
        runtime.execute(
            HardwareCommand(
                device_id=MUJOCO_PANDA_DEVICE_ID,
                lease_id=lease.lease_id,
                action="move_cartesian",
                parameters={"delta_xyz": [0.2, 0.0, 0.0], "steps": 200},
            ),
            run_id="mujoco-stop",
        )
    )

    await asyncio.wait_for(first_step.wait(), timeout=1)
    stopped = await runtime.stop(
        MUJOCO_PANDA_DEVICE_ID, reason="operator stop", run_id="mujoco-stop"
    )

    assert stopped.acknowledged is True
    with pytest.raises(asyncio.CancelledError):
        await execution
    await runtime.aclose()
