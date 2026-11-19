from __future__ import annotations

from pathlib import Path
import platform
import sys
from typing import Any, cast

import numpy as np
from pydantic import ValidationError
import pytest

from vibe.core.hardware import HardwareRuntime
from vibe.hardware_sim._perception import green_point_cloud, red_point_cloud
from vibe.hardware_sim.adapter import RobosuiteSimulatorAdapter
from vibe.hardware_sim.models import SimulatorSettings


def test_task_selection_is_explicit_and_default_stays_lift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ROBO_SIM_TASK", raising=False)
    assert SimulatorSettings.from_environment().task == "Lift"
    monkeypatch.setenv("ROBO_SIM_TASK", "Stack")
    assert SimulatorSettings.from_environment().task == "Stack"
    with pytest.raises(ValidationError):
        SimulatorSettings(task="unsupported")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="H1 off"):
        RobosuiteSimulatorAdapter(task="Stack", geometry_library="h1-v8-v9")


def test_colors_use_camera_only_and_keep_clusters_separate() -> None:
    rgb = np.zeros((20, 20, 3), dtype=np.uint8)
    rgb[1:8, 1:8, 0] = 255
    rgb[12:19, 12:19, 1] = 255
    depth = np.ones((20, 20))
    intrinsic = np.eye(3)
    pose = np.eye(4)
    red = red_point_cloud(rgb, depth, intrinsic, pose)
    green = green_point_cloud(rgb, depth, intrinsic, pose)
    assert red["centroid"] == [4.0, 4.0, 1.0]
    assert green["centroid"] == [15.0, 15.0, 1.0]
    assert green["version"] == "green-rgbd-v1"
    assert not green_point_cloud(rgb * 0, depth, intrinsic, pose)["ok"]


@pytest.mark.asyncio
async def test_real_stack_observation_and_independent_stability_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "platform",
        {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}[platform.system()],
    )
    pytest.importorskip("robosuite")
    adapter = RobosuiteSimulatorAdapter(
        task="Stack",
        show_viewer=False,
        observation_mode="rgbd",
        seed=1201,
        evidence_dir=tmp_path / "evidence",
    )
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    device = "mujoco-panda-1"
    try:
        snapshot = await runtime.connect(device, run_id="stack-test")
        assert snapshot.state["environment"] == "Stack"
        state: dict[str, Any] = snapshot.state
        for key in ("perception", "support_perception"):
            assert state[key]["ok"]
            assert state[key]["count"] <= 128
            assert state[key]["units"] == "m"
        assert (
            state["perception"]["rgbd_uri"] == state["support_perception"]["rgbd_uri"]
        )
        assert (
            not {
                "cube_position",
                "cubeA_pos",
                "cubeB_pos",
                "grasped",
                "stack_stable_cycles",
            }
            & state.keys()
        )
        assert (
            await runtime.verify(device, criterion="stack_objects", run_id="stack-test")
        ).status == "failed"
        # Evaluator-only positive fixture: never supplied to a policy or a model.
        env = adapter._environment
        assert env is not None
        green = env.sim.data.body_xpos[env.cubeB_body_id].copy()
        env.sim.data.set_joint_qpos(
            env.cubeA.joints[0], np.r_[green + [0, 0, 0.045], [1, 0, 0, 0]]
        )
        env.sim.forward()
        assert (
            await runtime.verify(device, criterion="stack_objects", run_id="stack-test")
        ).status == "failed"
        for _ in range(50):
            adapter._step_once(adapter._zero_action())
        passed = await runtime.verify(
            device, criterion="stack_objects", run_id="stack-test"
        )
        assert passed.status == "passed"
        cycles = passed.checks[1].observed
        assert isinstance(cycles, int) and cycles >= 20
        # A failed instantaneous predicate invalidates even a previously stable stack.
        monkeypatch.setattr(env, "_check_success", lambda: False)
        assert (
            await runtime.verify(device, criterion="stack_objects", run_id="stack-test")
        ).status == "failed"
        adapter._step_once(adapter._zero_action())
        assert adapter._stack_stable_cycles == 0
        cleanup = await runtime.bounded_cleanup()
        devices = cast(dict[str, Any], cleanup["devices"])
        assert devices[device]["verify"]["criterion"] == "stack_objects"
        assert devices[device]["stop"]["acknowledged"]
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_stack_task_and_cleanup_through_real_mcp(tmp_path: Path) -> None:
    from vibe.core.hardware import MCPStdioDeviceAdapter
    from vibe.core.tools.mcp.pool import MCPConnectionPool

    pool = MCPConnectionPool()
    adapter = MCPStdioDeviceAdapter(
        adapter_id="robo-stack",
        command=[sys.executable, "-m", "vibe.hardware_sim.entrypoint"],
        pool=pool,
        env={
            "ROBO_SIM_TASK": "Stack",
            "ROBO_SIM_HEADLESS": "1",
            "ROBO_SIM_OBSERVATION_MODE": "rgbd",
            "ROBO_SIM_GEOMETRY_LIBRARY": "off",
            "ROBO_SIM_SEED": "1201",
        },
        cwd=str(Path.cwd()),
        startup_timeout_sec=20,
        tool_timeout_sec=20,
    )
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    try:
        manifest = (await runtime.discover())[0]
        assert manifest.metadata["environment"] == "Stack"
        assert [v.name for v in manifest.verifications] == ["stack_objects"]
        snapshot = await runtime.connect("mujoco-panda-1", run_id="stack-mcp")
        assert "support_perception" in snapshot.state
        await runtime.acquire_lease("mujoco-panda-1", owner="test", run_id="stack-mcp")
        cleanup = await runtime.bounded_cleanup()
        devices = cast(dict[str, Any], cleanup["devices"])
        assert devices["mujoco-panda-1"]["verify"]["criterion"] == "stack_objects"
        assert devices["mujoco-panda-1"]["verify"]["status"] == "failed"
        assert devices["mujoco-panda-1"]["release"] is True
    finally:
        await runtime.aclose()
        await pool.aclose()
