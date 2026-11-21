from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from vibe.core.hardware import (
    HardwarePolicyError,
    HardwarePolicyRunner,
    HardwareRuntime,
)
from vibe.core.tools.builtins.robo import RoboRunPolicyConfig


def test_model_receives_policy_budget_and_snapshot_contract() -> None:
    agent = build_test_agent_loop(
        config=build_test_vibe_config(enabled_tools=["robo_run_policy"])
    )
    spec = agent.format_handler.get_available_tools(agent.tool_manager)[0]
    description = spec.function.description
    defaults = RoboRunPolicyConfig()
    assert defaults.max_statements is None
    assert defaults.max_actions is None
    assert defaults.max_loop_iterations is None
    assert defaults.timeout_seconds is None
    assert "默认不限制" in description
    assert "200 条执行语句" not in description
    assert '["state"]["perception"]' in description
    assert 'receipt["result"]["motion"]["reached"]' in description
    assert 'receipt["motion"]' in description
    assert 'act("move_to", ...)' in description
    assert "CommandReceipt" in description


@pytest.mark.asyncio
async def test_documented_statement_cost_and_per_policy_reset(tmp_path: Path) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    await runtime.connect("sim-arm-1", run_id="count")
    lease = await runtime.acquire_lease("sim-arm-1", owner="count", run_id="count")
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="count")
    # Assignment + for header + 2 * (augassign + if + print) = 8.
    # Conditions and implicit loop-variable assignment do not add statements.
    source = "n = 0\nfor k in range(2):\n    n += 1\n    if n > 0:\n        print(n)"
    runner = HardwarePolicyRunner(runtime, artifact_dir=tmp_path, max_statements=8)
    try:
        for _ in range(2):
            result = await runner.run(
                source, device_id="sim-arm-1", lease_id=lease.lease_id, run_id="count"
            )
            assert result.stdout == ["1", "2"]
        short = HardwarePolicyRunner(runtime, artifact_dir=tmp_path, max_statements=7)
        with pytest.raises(HardwarePolicyError) as exc:
            await short.run(
                source, device_id="sim-arm-1", lease_id=lease.lease_id, run_id="count"
            )
        assert exc.value.code == "statement_limit"
        assert exc.value.line == 5
    finally:
        await runtime.aclose()
