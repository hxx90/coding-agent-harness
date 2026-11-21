from __future__ import annotations

from pathlib import Path
import platform
import sys
from typing import Any, cast

import numpy as np
import pytest

from vibe.core.hardware import HardwareCommand, HardwareRuntime, MCPStdioDeviceAdapter
from vibe.core.tools.mcp.pool import MCPConnectionPool
from vibe.hardware_sim._pickplace import pickplace_goal
from vibe.hardware_sim.adapter import RobosuiteSimulatorAdapter
from vibe.hardware_sim.models import SimulatorSettings


def test_pickplace_selection_requires_explicit_camera_contract(monkeypatch):
    monkeypatch.setenv("ROBO_SIM_TASK", "PickPlace")
    assert SimulatorSettings.from_environment().task == "PickPlace"
    with pytest.raises(ValueError, match="RGB-D"):
        RobosuiteSimulatorAdapter(task="PickPlace")
    with pytest.raises(ValueError, match="H1 off"):
        RobosuiteSimulatorAdapter(
            task="PickPlace", observation_mode="rgbd", geometry_library="h1-v8-v9"
        )


@pytest.mark.asyncio
async def test_real_pickplace_camera_and_independent_verifier(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "platform",
        {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}[platform.system()],
    )
    pytest.importorskip("robosuite")
    adapter = RobosuiteSimulatorAdapter(
        task="PickPlace",
        observation_mode="rgbd",
        show_viewer=False,
        seed=1301,
        evidence_dir=tmp_path / "evidence",
    )
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    device = "mujoco-panda-1"
    try:
        snapshot = await runtime.connect(device, run_id="pickplace-test")
        state: dict[str, Any] = snapshot.state
        assert state["perception"]["ok"]
        assert state["perception"]["count"] <= 128
        assert state["perception"]["units"] == "m"
        assert state["task_goal"] == pickplace_goal()
        assert (
            not {
                "Can_pos",
                "cube_position",
                "grasped",
                "objects_in_bins",
                "pickplace_stable_cycles",
                "object-state",
            }
            & state.keys()
        )
        env = adapter._environment
        assert env is not None
        np.testing.assert_allclose(
            env.target_bin_placements[env.object_id, :2],
            state["task_goal"]["center_xy"],
        )
        assert (
            await runtime.verify(
                device, criterion="place_object", run_id="pickplace-test"
            )
        ).status == "failed"
        with pytest.raises(Exception, match="Unsupported.*criterion"):
            await runtime.verify(
                device, criterion="lift_object", run_id="pickplace-test"
            )

        # Scorer-only fixtures. Never sent to a policy or model.
        can = env.objects[env.object_id]
        env.sim.data.set_joint_qpos(can.joints[0], [0.0025, 0.1575, 0.88, 1, 0, 0, 0])
        env.sim.forward()
        for _ in range(40):
            adapter._step_once(adapter._zero_action())
        assert (
            await runtime.verify(
                device, criterion="place_object", run_id="pickplace-test"
            )
        ).status == "failed"
        env.sim.data.set_joint_qpos(can.joints[0], [0.1975, 0.4025, 0.88, 1, 0, 0, 0])
        env.sim.data.set_joint_qvel(can.joints[0], np.zeros(6))
        env.sim.forward()
        assert (
            await runtime.verify(
                device, criterion="place_object", run_id="pickplace-test"
            )
        ).status == "failed"
        for _ in range(50):
            adapter._step_once(adapter._zero_action())
        before_cycles = adapter._control_cycles_used
        passed = await runtime.verify(
            device, criterion="place_object", run_id="pickplace-test"
        )
        assert passed.status == "passed"
        assert adapter._control_cycles_used == before_cycles  # verify cannot settle it
        assert cast(int, passed.checks[1].observed) >= 20
        with monkeypatch.context() as patch:
            patch.setattr(env, "_check_grasp", lambda **kwargs: True)
            assert (
                await runtime.verify(
                    device, criterion="place_object", run_id="pickplace-test"
                )
            ).status == "failed"
        shifted = env.sim.data.get_joint_qpos(can.joints[0]).copy()
        shifted[0] += 0.01
        env.sim.data.set_joint_qpos(can.joints[0], shifted)
        env.sim.forward()
        assert (
            await runtime.verify(
                device, criterion="place_object", run_id="pickplace-test"
            )
        ).status == "failed"
        await adapter._apply_action("home", {})
        assert adapter._pickplace_stability.cycles == 0
        assert adapter._control_cycles_used == before_cycles
        assert (
            await runtime.verify(
                device, criterion="place_object", run_id="pickplace-test"
            )
        ).status == "failed"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_pickplace_manifest_and_cleanup_through_real_mcp(tmp_path):
    pool = MCPConnectionPool()
    adapter = MCPStdioDeviceAdapter(
        adapter_id="robo-pickplace",
        command=[sys.executable, "-m", "vibe.hardware_sim.entrypoint"],
        pool=pool,
        env={
            "ROBO_SIM_TASK": "PickPlace",
            "ROBO_SIM_HEADLESS": "1",
            "ROBO_SIM_OBSERVATION_MODE": "rgbd",
            "ROBO_SIM_GEOMETRY_LIBRARY": "off",
            "ROBO_SIM_SEED": "1301",
        },
        cwd=str(Path.cwd()),
        startup_timeout_sec=20,
        tool_timeout_sec=20,
    )
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    try:
        manifest = (await runtime.discover())[0]
        assert manifest.metadata["environment"] == "PickPlace"
        assert [v.name for v in manifest.verifications] == ["place_object"]
        snapshot = await runtime.connect("mujoco-panda-1", run_id="pickplace-mcp")
        assert snapshot.state["task_goal"] == pickplace_goal()
        lease = await runtime.acquire_lease(
            "mujoco-panda-1", owner="test", run_id="pickplace-mcp"
        )
        await runtime.arm(
            "mujoco-panda-1", lease_id=lease.lease_id, run_id="pickplace-mcp"
        )
        target = cast(list[float], snapshot.state["end_effector_position"]).copy()
        target[2] += 0.02
        receipt = await runtime.execute(
            HardwareCommand(
                device_id="mujoco-panda-1",
                lease_id=lease.lease_id,
                action="move_to",
                parameters={"target_xyz": [target[0], target[1], target[2]]},
            ),
            run_id="pickplace-mcp",
        )
        assert receipt.status == "completed"
        assert cast(dict[str, Any], receipt.result["motion"])["reached"]
        cleanup = await runtime.bounded_cleanup()
        devices = cast(dict[str, Any], cleanup["devices"])
        assert devices["mujoco-panda-1"]["verify"]["criterion"] == "place_object"
        assert devices["mujoco-panda-1"]["verify"]["status"] == "failed"
        assert devices["mujoco-panda-1"]["stop"]["acknowledged"]
        assert devices["mujoco-panda-1"]["release"] is True
    finally:
        await runtime.aclose()
        await pool.aclose()
