from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from vibe.core.hardware import HardwareCommand, HardwareRuntime, MCPStdioDeviceAdapter
from vibe.core.tools.mcp.pool import MCPConnectionPool
from vibe.hardware_sim import MUJOCO_PANDA_DEVICE_ID

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("robosuite") is None, reason="requires the simulator extra"
)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_runtime_controls_mujoco_through_stdio_mcp(tmp_path: Path) -> None:
    pool = MCPConnectionPool()
    evidence_dir = tmp_path / "evidence"
    recording_dir = tmp_path / "recordings"
    adapter = MCPStdioDeviceAdapter(
        adapter_id="robo-mujoco",
        command=[sys.executable, "-m", "vibe.hardware_sim.entrypoint"],
        pool=pool,
        env={
            "ROBO_SIM_HEADLESS": "1",
            "ROBO_EVIDENCE_DIR": str(evidence_dir),
            "ROBO_RECORDING_DIR": str(recording_dir),
        },
        cwd=str(Path.cwd()),
        startup_timeout_sec=20,
        tool_timeout_sec=20,
    )
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)

    try:
        manifests = await runtime.discover()
        await runtime.connect(MUJOCO_PANDA_DEVICE_ID, run_id="mujoco-mcp")
        lease = await runtime.acquire_lease(
            MUJOCO_PANDA_DEVICE_ID, owner="test", run_id="mujoco-mcp"
        )
        await runtime.arm(
            MUJOCO_PANDA_DEVICE_ID, lease_id=lease.lease_id, run_id="mujoco-mcp"
        )
        receipt = await runtime.execute(
            HardwareCommand(
                device_id=MUJOCO_PANDA_DEVICE_ID,
                lease_id=lease.lease_id,
                action="move_cartesian",
                parameters={"delta_xyz": [0.1, 0.0, 0.0], "steps": 5},
            ),
            run_id="mujoco-mcp",
        )
        snapshot = await runtime.observe(MUJOCO_PANDA_DEVICE_ID, run_id="mujoco-mcp")
        verification = await runtime.verify(
            MUJOCO_PANDA_DEVICE_ID, criterion="lift_object", run_id="mujoco-mcp"
        )
        stopped = await runtime.stop(
            MUJOCO_PANDA_DEVICE_ID, reason="test complete", run_id="mujoco-mcp"
        )
    finally:
        await runtime.aclose()
        await pool.aclose()

    assert [manifest.adapter_id for manifest in manifests] == ["robo-mujoco"]
    assert receipt.status == "completed"
    assert snapshot.state["viewer"] is False
    assert len(snapshot.evidence) == 1
    assert Path(snapshot.evidence[0].uri.removeprefix("file://")).is_relative_to(
        evidence_dir
    )
    assert verification.status == "failed"
    assert len(verification.evidence) == 1
    assert verification.evidence_ids == [verification.evidence[0].evidence_id]
    assert all(
        check.evidence_ids == verification.evidence_ids for check in verification.checks
    )
    assert stopped.acknowledged is True
    recordings = list(recording_dir.glob("*.mp4"))
    assert len(recordings) == 1
    assert recordings[0].stat().st_size > 0


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_rgbd_h1_is_available_through_mcp_and_bounded_policy(
    tmp_path: Path,
) -> None:
    from vibe.core.hardware import HardwarePolicyRunner

    pool = MCPConnectionPool()
    adapter = MCPStdioDeviceAdapter(
        adapter_id="robo-mujoco",
        command=[sys.executable, "-m", "vibe.hardware_sim.entrypoint"],
        pool=pool,
        env={
            "ROBO_SIM_HEADLESS": "1",
            "ROBO_SIM_OBSERVATION_MODE": "rgbd",
            "ROBO_SIM_GEOMETRY_LIBRARY": "h1-v8-v9",
            "ROBO_SIM_SEED": "117",
            "ROBO_EVIDENCE_DIR": str(tmp_path / "evidence"),
        },
        cwd=str(Path.cwd()),
        startup_timeout_sec=20,
        tool_timeout_sec=20,
    )
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    try:
        manifest = (await runtime.discover())[0]
        assert "resting_geometry" in str(manifest.metadata["geometry_notes"])
        await runtime.connect(MUJOCO_PANDA_DEVICE_ID, run_id="h1-mcp")
        lease = await runtime.acquire_lease(
            MUJOCO_PANDA_DEVICE_ID, owner="test", run_id="h1-mcp"
        )
        await runtime.arm(
            MUJOCO_PANDA_DEVICE_ID, lease_id=lease.lease_id, run_id="h1-mcp"
        )
        result = await HardwarePolicyRunner(
            runtime, artifact_dir=tmp_path / "policies"
        ).run(
            's = observe()\ng = resting_geometry(s["state"]["perception"]["points"])\nprint(g["ok"])\nverify("lift_object")',
            device_id=MUJOCO_PANDA_DEVICE_ID,
            lease_id=lease.lease_id,
            run_id="h1-mcp",
        )
        assert result.stdout == ["true"]
        assert result.geometry_library == "h1-v8-v9"
        assert result.task_outcome == "failed"
        assert "cube_position" not in result.snapshot.state
        assert sum(step.kind == "capability" for step in result.steps) == 1
    finally:
        await runtime.aclose()
        await pool.aclose()
