from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import JsonValue

from vibe.core.hardware import HardwareCommand
from vibe.hardware_sim.adapter import RobosuiteSimulatorAdapter
from vibe.hardware_sim.models import SimulatorSettings


def create_server(adapter: RobosuiteSimulatorAdapter) -> FastMCP:
    server = FastMCP("Robo MuJoCo Simulator", log_level="WARNING")

    @server.tool(name="robo_devices", structured_output=True)
    async def list_devices() -> dict[str, Any]:
        devices = await adapter.discover()
        return {"devices": [device.model_dump(mode="json") for device in devices]}

    @server.tool(name="robo_connect", structured_output=True)
    async def connect(device_id: str) -> dict[str, Any]:
        return (await adapter.connect(device_id)).model_dump(mode="json")

    @server.tool(name="robo_arm", structured_output=True)
    async def arm(device_id: str) -> dict[str, Any]:
        return (await adapter.arm(device_id)).model_dump(mode="json")

    @server.tool(name="robo_disarm", structured_output=True)
    async def disarm(device_id: str) -> dict[str, Any]:
        return (await adapter.disarm(device_id)).model_dump(mode="json")

    @server.tool(name="robo_observe", structured_output=True)
    async def observe(device_id: str) -> dict[str, Any]:
        return (await adapter.observe(device_id)).model_dump(mode="json")

    @server.tool(name="robo_verify", structured_output=True)
    async def verify(
        device_id: str, criterion: str, parameters: dict[str, JsonValue] | None = None
    ) -> dict[str, Any]:
        return (
            await adapter.verify(device_id, criterion, parameters or {})
        ).model_dump(mode="json")

    @server.tool(name="robo_execute", structured_output=True)
    async def execute(
        command_id: str,
        device_id: str,
        lease_id: str,
        action: str,
        parameters: dict[str, JsonValue] | None = None,
    ) -> dict[str, Any]:
        command = HardwareCommand(
            command_id=command_id,
            device_id=device_id,
            lease_id=lease_id,
            action=action,
            parameters=parameters or {},
        )
        return (await adapter.execute(command)).model_dump(mode="json")

    @server.tool(name="robo_stop", structured_output=True)
    async def stop(device_id: str, reason: str) -> dict[str, Any]:
        return (await adapter.stop(device_id, reason)).model_dump(mode="json")

    @server.tool(name="robo_disconnect", structured_output=True)
    async def disconnect(device_id: str) -> dict[str, Any]:
        await adapter.disconnect(device_id)
        return {"ok": True}

    return server


def run_server(settings: SimulatorSettings) -> None:
    adapter = RobosuiteSimulatorAdapter(
        show_viewer=not settings.headless, evidence_dir=settings.evidence_dir
    )
    try:
        create_server(adapter).run(transport="stdio")
    finally:
        asyncio.run(adapter.aclose())


__all__ = ["create_server", "run_server"]
