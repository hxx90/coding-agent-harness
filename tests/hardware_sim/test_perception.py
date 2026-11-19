from __future__ import annotations

import platform
import sys
from typing import Any, cast

import numpy as np
import pytest

from vibe.core.hardware import HardwareCommand, HardwareRuntime
from vibe.hardware_sim._perception import red_point_cloud
from vibe.hardware_sim.adapter import RobosuiteSimulatorAdapter


def test_unprojection_uses_optical_depth_and_camera_to_world():
    rgb = np.zeros((10, 10, 3), dtype=np.uint8)
    rgb[2:8, 2:8, 0] = 255
    depth = np.ones((10, 10)) * 2
    intrinsic = np.array([[10, 0, 5], [0, 10, 5], [0, 0, 1]])
    pose = np.eye(4)
    pose[:3, 3] = [1, 2, 3]
    value = red_point_cloud(rgb, depth, intrinsic, pose)
    assert value["ok"]
    assert value["centroid"] == pytest.approx([0.9, 1.9, 5])
    assert value["points"][0] == pytest.approx([0.4, 1.4, 5])
    assert not red_point_cloud(rgb * 0, depth, intrinsic, pose)["ok"]


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_rgbd_has_no_object_truth_and_refreshes_after_motion(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        sys,
        "platform",
        {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}[platform.system()],
    )
    initial_states = []
    initial_perception = []
    for library in ("off", "h1-v8-v9"):
        directory = tmp_path / library
        adapter = RobosuiteSimulatorAdapter(
            show_viewer=False,
            observation_mode="rgbd",
            geometry_library=library,
            seed=117,
            evidence_dir=directory / "evidence",
        )
        runtime = HardwareRuntime(adapters=[adapter], trace_dir=directory)
        try:
            snapshot = await runtime.connect("mujoco-panda-1", run_id="rgbd")
            assert (
                not {
                    "cube_position",
                    "grasped",
                    "cube_height_above_table_m",
                    "gripper_to_cube_distance_m",
                    "cube_displacement_from_home_m",
                }
                & snapshot.state.keys()
            )
            assert adapter._environment is not None
            initial_states.append(adapter._environment.sim.get_state().flatten())
            perception = cast(dict[str, Any], snapshot.state["perception"])
            initial_perception.append(perception["points"])
            assert perception["ok"]
            # Scorer-only calibration audit: a visually reconstructed cluster must
            # land near the known physical cube, never feed this reference to actor.
            assert (
                np.linalg.norm(
                    np.array(perception["centroid"]) - adapter._float_values("cube_pos")
                )
                < 0.025
            )
            old_depth = np.load(perception["rgbd_uri"].removeprefix("file://"))["depth"]
            assert old_depth.shape == (384, 384)
            lease = await runtime.acquire_lease(
                "mujoco-panda-1", owner="test", run_id="rgbd"
            )
            await runtime.arm("mujoco-panda-1", lease_id=lease.lease_id, run_id="rgbd")
            receipt = await runtime.execute(
                HardwareCommand(
                    device_id="mujoco-panda-1",
                    lease_id=lease.lease_id,
                    action="move_cartesian",
                    parameters={"delta_xyz": [0.1, 0, 0], "steps": 2},
                ),
                run_id="rgbd",
            )
            assert "cube_position" not in cast(dict[str, Any], receipt.result["state"])
            after = await runtime.observe("mujoco-panda-1", run_id="rgbd")
            assert (
                cast(dict[str, Any], after.state["perception"])["sequence"]
                > perception["sequence"]
            )
            assert (
                await runtime.verify(
                    "mujoco-panda-1", criterion="lift_object", run_id="rgbd"
                )
            ).status == "failed"
        finally:
            await runtime.aclose()
    np.testing.assert_array_equal(initial_states[0], initial_states[1])
    assert initial_perception[0] == initial_perception[1]
