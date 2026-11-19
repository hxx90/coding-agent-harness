from __future__ import annotations

import json

import pytest

from vibe.core.hardware import (
    HardwarePolicyError,
    HardwarePolicyRunner,
    HardwareRuntime,
)
from vibe.core.hardware._geometry import H1_VERSION, checked_h1, resting_geometry
from vibe.core.hardware.simulator import DeterministicSimulatorAdapter


def points():
    return [
        [x / 1000, y / 1000, 0.84]
        for x in range(-20, 21, 10)
        for y in range(-20, 21, 10)
    ]


def test_frozen_h1_produces_json_and_rejects_unbounded_inputs():
    assert set(checked_h1()) == {"capability.py", "CAPABILITY.md"}
    result = resting_geometry(points(), support_z=0.80)
    assert result["ok"]
    assert result["grasp"][2] == pytest.approx(0.826)
    json.dumps(result, allow_nan=False)
    for invalid in (
        [[0, 0, float("nan")]],
        [[0, 0, 0]] * 129,
        [[True, 0, 0]],
        [[0, 0, 0, 0]],
    ):
        with pytest.raises(ValueError):
            resting_geometry(invalid)


def test_hash_mismatch_fails_before_import(tmp_path, monkeypatch):
    from vibe.core.hardware import _geometry

    (tmp_path / "bundle.json").write_text("{}")
    monkeypatch.setattr(_geometry, "_ASSETS", tmp_path)
    with pytest.raises(ValueError, match="hash mismatch"):
        resting_geometry(points())


@pytest.mark.asyncio
async def test_helper_requires_manifest_and_is_recorded_without_task_success(tmp_path):
    class GeometrySimulator(DeterministicSimulatorAdapter):
        enabled = False

        async def discover(self):
            manifests = await super().discover()
            if self.enabled:
                manifests[0].metadata["geometry_library"] = H1_VERSION
            return manifests

    adapter = GeometrySimulator()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.connect("sim-arm-1", run_id="geometry")
    lease = await runtime.acquire_lease("sim-arm-1", owner="test", run_id="geometry")
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="geometry")
    runner = HardwarePolicyRunner(runtime, artifact_dir=tmp_path / "policies")
    code = f'g = resting_geometry({points()!r})\nprint(g["ok"])'
    try:
        with pytest.raises(HardwarePolicyError, match="allowed function"):
            await runner.run(
                code, device_id="sim-arm-1", lease_id=lease.lease_id, run_id="geometry"
            )
        adapter.enabled = True
        result = await runner.run(
            code, device_id="sim-arm-1", lease_id=lease.lease_id, run_id="geometry"
        )
        assert result.geometry_library == H1_VERSION
        assert result.steps[0].kind == "capability"
        assert result.task_outcome == "not_verified"
        assert not result.receipts
        assert (tmp_path / "policies" / f"{result.policy_id}.json").exists()
    finally:
        await runtime.aclose()
