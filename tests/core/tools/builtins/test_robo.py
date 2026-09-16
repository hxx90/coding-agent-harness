from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from pydantic import BaseModel
import pytest

from vibe.core.hardware import DeterministicSimulatorAdapter, HardwareRuntime
from vibe.core.tools.base import InvokeContext, ToolError
from vibe.core.tools.builtins.robo import (
    RoboDevices,
    RoboDevicesArgs,
    RoboDevicesConfig,
    RoboExecute,
    RoboExecuteArgs,
    RoboExecuteConfig,
    RoboObserve,
    RoboObserveArgs,
    RoboObserveConfig,
    RoboStop,
    RoboStopArgs,
    RoboStopConfig,
)


async def _result(stream: AsyncGenerator[object, None]) -> BaseModel:
    items = [item async for item in stream]
    assert len(items) == 1
    assert isinstance(items[0], BaseModel)
    return items[0]


class FailingDiscoveryAdapter(DeterministicSimulatorAdapter):
    async def discover(self):
        raise RuntimeError("vendor offline")


@pytest.mark.asyncio
async def test_robo_tools_share_the_session_hardware_runtime(tmp_path: Path) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    context = InvokeContext(
        tool_call_id="tool-1", session_id="session-1", hardware_runtime=runtime
    )
    devices = RoboDevices.from_config(lambda: RoboDevicesConfig())
    execute = RoboExecute.from_config(lambda: RoboExecuteConfig())
    observe = RoboObserve.from_config(lambda: RoboObserveConfig(), cwd=tmp_path)
    stop = RoboStop.from_config(lambda: RoboStopConfig())

    listed = await _result(devices.run(RoboDevicesArgs(action="list"), context))
    assert listed.model_dump()["devices"][0]["device_id"] == "sim-arm-1"

    await _result(
        devices.run(
            RoboDevicesArgs(action="connect", device_id="sim-arm-1", run_id="tool-run"),
            context,
        )
    )
    leased = await _result(
        devices.run(
            RoboDevicesArgs(
                action="acquire",
                device_id="sim-arm-1",
                owner="agent",
                run_id="tool-run",
            ),
            context,
        )
    )
    lease_id = leased.model_dump()["lease"]["lease_id"]
    armed = await _result(
        devices.run(
            RoboDevicesArgs(
                action="arm",
                device_id="sim-arm-1",
                lease_id=lease_id,
                run_id="tool-run",
            ),
            context,
        )
    )
    completed = await _result(
        execute.run(
            RoboExecuteArgs(
                device_id="sim-arm-1",
                lease_id=lease_id,
                action="fold_cloth",
                parameters={"cloth": "shirt"},
                run_id="tool-run",
            ),
            context,
        )
    )
    observed = await _result(
        observe.run(RoboObserveArgs(device_id="sim-arm-1", run_id="tool-run"), context)
    )
    stopped = await _result(
        stop.run(
            RoboStopArgs(device_id="sim-arm-1", reason="done", run_id="tool-run"),
            context,
        )
    )

    assert completed.model_dump()["receipt"]["status"] == "completed"
    assert armed.model_dump()["snapshot"]["status"] == "armed"
    assert observed.model_dump()["snapshot"]["state"]["folded_cloths"] == ["shirt"]
    assert stopped.model_dump()["receipt"]["acknowledged"] is True
    arm_permission = devices.resolve_permission(RoboDevicesArgs(action="arm"))
    assert arm_permission is not None
    assert arm_permission.permission == "ask"
    assert RoboExecuteConfig().permission == "ask"
    assert RoboStopConfig().permission == "always"

    await runtime.aclose()


@pytest.mark.asyncio
async def test_robo_tool_normalizes_adapter_failures(tmp_path: Path) -> None:
    runtime = HardwareRuntime(adapters=[FailingDiscoveryAdapter()], trace_dir=tmp_path)
    context = InvokeContext(tool_call_id="tool-1", hardware_runtime=runtime)
    devices = RoboDevices.from_config(lambda: RoboDevicesConfig())

    with pytest.raises(ToolError, match="Hardware adapter discover failed"):
        await _result(devices.run(RoboDevicesArgs(action="list"), context))

    await runtime.aclose()
