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
    adapter = MCPStdioDeviceAdapter(
        adapter_id="robo-mujoco",
        command=[sys.executable, "-m", "vibe.hardware_sim.entrypoint"],
        pool=pool,
        env={"ROBO_SIM_HEADLESS": "1", "ROBO_EVIDENCE_DIR": str(evidence_dir)},
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
