from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibe.core.config import MCPStdio
from vibe.core.hardware._adapter_port import DeviceAdapter
from vibe.core.hardware.mcp_adapter import MCPDeviceToolNames, MCPStdioDeviceAdapter
from vibe.core.hardware.runtime import HardwareRuntime
from vibe.core.hardware.simulator import DeterministicSimulatorAdapter
from vibe.observability.logging import logger

if TYPE_CHECKING:
    from vibe.core.config import VibeConfigSchema
    from vibe.core.tools.mcp.pool import MCPConnectionPool


def create_hardware_runtime(
    config: VibeConfigSchema,
    *,
    mcp_pool: MCPConnectionPool | None,
    cwd: Path,
    session_id: str,
    session_dir: Path | None,
) -> HardwareRuntime:
    adapters: list[DeviceAdapter] = [DeterministicSimulatorAdapter()]
    if mcp_pool is not None:
        adapters.extend(_mcp_adapters(config, mcp_pool))
    trace_root = (
        session_dir / "hardware-runs"
        if session_dir is not None
        else cwd / ".vibe" / "hardware-runs" / session_id
    )
    return HardwareRuntime(adapters=adapters, trace_dir=trace_root)


def _mcp_adapters(
    config: VibeConfigSchema, pool: MCPConnectionPool
) -> list[DeviceAdapter]:
    names = MCPDeviceToolNames()
    required_hidden_tools = {
        names.discover,
        names.connect,
        names.arm,
        names.disarm,
        names.observe,
        names.execute,
        names.stop,
        names.disconnect,
    }
    adapters: list[DeviceAdapter] = []
    for server in config.mcp_servers:
        if (
            not isinstance(server, MCPStdio)
            or server.disabled
            or not server.name.startswith("robo-")
        ):
            continue
        if not required_hidden_tools.issubset(server.disabled_tools):
            logger.warning(
                "Skipping Robo MCP adapter %s because its raw device tools are not disabled",
                server.name,
            )
            continue
        adapters.append(
            MCPStdioDeviceAdapter(
                adapter_id=server.name,
                command=server.argv(),
                pool=pool,
                env=server.env,
                cwd=server.cwd,
                startup_timeout_sec=server.startup_timeout_sec,
                tool_timeout_sec=server.tool_timeout_sec,
            )
        )
    return adapters


__all__ = ["create_hardware_runtime"]
