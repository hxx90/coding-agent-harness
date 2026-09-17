from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from vibe.core.hardware import HardwareCommand
from vibe.core.hardware.mcp_adapter import MCPStdioDeviceAdapter


@dataclass
class FakeMCPResult:
    structured: dict[str, Any] | None
    text: str | None = None


class FakeMCPPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.interrupts: list[tuple[str, bool]] = []

    async def call_tool(self, **kwargs: Any) -> FakeMCPResult:
        tool_name = kwargs["tool_name"]
        arguments = kwargs["arguments"]
        self.calls.append((tool_name, arguments))
        self.interrupts.append((tool_name, False))
        responses = {
            "robo_devices": {
                "devices": [
                    {
                        "device_id": "vendor-arm-9",
                        "manufacturer": "Universal",
                        "model": "UR5e",
                        "kind": "robot_arm",
                        "actions": [
                            {
                                "name": "home",
                                "description": "Move home",
                                "destructive": True,
                            }
                        ],
                    }
                ]
            },
            "robo_connect": {
                "device_id": "vendor-arm-9",
                "status": "connected",
                "sequence": 1,
                "observed_at": "2026-09-17T00:00:00Z",
                "state": {"mode": "idle"},
            },
            "robo_arm": {
                "device_id": "vendor-arm-9",
                "status": "armed",
                "sequence": 2,
                "observed_at": "2026-09-17T00:00:01Z",
                "state": {"mode": "armed"},
            },
            "robo_disarm": {
                "device_id": "vendor-arm-9",
                "status": "connected",
                "sequence": 3,
                "observed_at": "2026-09-17T00:00:03Z",
                "state": {"mode": "idle"},
            },
            "robo_execute": {
                "command_id": "command-9",
                "device_id": "vendor-arm-9",
                "status": "completed",
                "started_at": "2026-09-17T00:00:01Z",
                "completed_at": "2026-09-17T00:00:02Z",
                "result": {"pose": "home"},
            },
            "robo_observe": {
                "device_id": "vendor-arm-9",
                "status": "connected",
                "sequence": 2,
                "observed_at": "2026-09-17T00:00:02Z",
                "state": {"mode": "idle"},
            },
            "robo_stop": {
                "device_id": "vendor-arm-9",
                "acknowledged": True,
                "observed_at": "2026-09-17T00:00:03Z",
                "message": "controller stopped",
            },
            "robo_disconnect": {"ok": True},
        }
        return FakeMCPResult(structured=responses[tool_name])

    async def call_interrupt_tool(self, **kwargs: Any) -> FakeMCPResult:
        result = await self.call_tool(**kwargs)
        self.interrupts[-1] = (kwargs["tool_name"], True)
        return result


@pytest.mark.asyncio
async def test_mcp_adapter_translates_vendor_tools_into_hardware_contract() -> None:
    pool = FakeMCPPool()
    adapter = MCPStdioDeviceAdapter(
        adapter_id="robo-universal", command=["vendor-mcp"], pool=pool
    )

    manifests = await adapter.discover()
    connected = await adapter.connect("vendor-arm-9")
    armed = await adapter.arm("vendor-arm-9")
    receipt = await adapter.execute(
        HardwareCommand(
            command_id="command-9",
            device_id="vendor-arm-9",
            lease_id="lease-9",
            action="home",
        )
    )
    snapshot = await adapter.observe("vendor-arm-9")
    stopped = await adapter.stop("vendor-arm-9", "operator request")
    disarmed = await adapter.disarm("vendor-arm-9")
    await adapter.disconnect("vendor-arm-9")

    assert manifests[0].adapter_id == "robo-universal"
    assert manifests[0].transport == "mcp_stdio"
    assert connected.state == {"mode": "idle"}
    assert armed.status == "armed"
    assert receipt.result == {"pose": "home"}
    assert snapshot.sequence == 2
    assert stopped.acknowledged is True
    assert disarmed.status == "connected"
    assert pool.calls == [
        ("robo_devices", {}),
        ("robo_connect", {"device_id": "vendor-arm-9"}),
        ("robo_arm", {"device_id": "vendor-arm-9"}),
        (
            "robo_execute",
            {
                "command_id": "command-9",
                "device_id": "vendor-arm-9",
                "lease_id": "lease-9",
                "action": "home",
                "parameters": {},
            },
        ),
        ("robo_observe", {"device_id": "vendor-arm-9"}),
        ("robo_stop", {"device_id": "vendor-arm-9", "reason": "operator request"}),
        ("robo_disarm", {"device_id": "vendor-arm-9"}),
        ("robo_disconnect", {"device_id": "vendor-arm-9"}),
    ]
    assert ("robo_stop", True) in pool.interrupts
    assert all(
        interrupt is False for name, interrupt in pool.interrupts if name != "robo_stop"
    )
