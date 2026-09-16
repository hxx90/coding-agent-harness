from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from vibe.core.hardware._mcp_port import MCPCallPort
from vibe.core.hardware.models import (
    CommandReceipt,
    DeviceCapabilityManifest,
    DeviceSnapshot,
    DeviceTransport,
    HardwareCommand,
    StopReceipt,
)


class MCPAdapterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MCPDeviceToolNames:
    discover: str = "robo_devices"
    connect: str = "robo_connect"
    arm: str = "robo_arm"
    disarm: str = "robo_disarm"
    observe: str = "robo_observe"
    execute: str = "robo_execute"
    stop: str = "robo_stop"
    disconnect: str = "robo_disconnect"


class _DeviceList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    devices: list[dict[str, Any]]


class MCPStdioDeviceAdapter:
    def __init__(
        self,
        *,
        adapter_id: str,
        command: list[str],
        pool: MCPCallPort,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        startup_timeout_sec: float | None = None,
        tool_timeout_sec: float | None = None,
        tools: MCPDeviceToolNames | None = None,
    ) -> None:
        if not command:
            raise ValueError("An MCP stdio adapter requires a command")
        self._adapter_id = adapter_id
        self._command = list(command)
        self._pool = pool
        self._env = dict(env or {})
        self._cwd = cwd
        self._startup_timeout_sec = startup_timeout_sec
        self._tool_timeout_sec = tool_timeout_sec
        self._tools = tools or MCPDeviceToolNames()

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    async def discover(self) -> list[DeviceCapabilityManifest]:
        payload = await self._call(self._tools.discover, {})
        try:
            device_list = _DeviceList.model_validate(payload)
            return [
                DeviceCapabilityManifest.model_validate({
                    **device,
                    "adapter_id": self.adapter_id,
                    "transport": DeviceTransport.MCP_STDIO,
                })
                for device in device_list.devices
            ]
        except ValidationError as exc:
            raise MCPAdapterError(
                "MCP device list does not match the Robo contract"
            ) from exc

    async def connect(self, device_id: str) -> DeviceSnapshot:
        return await self._call_model(
            self._tools.connect, {"device_id": device_id}, DeviceSnapshot
        )

    async def arm(self, device_id: str) -> DeviceSnapshot:
        return await self._call_model(
            self._tools.arm, {"device_id": device_id}, DeviceSnapshot
        )

    async def disarm(self, device_id: str) -> DeviceSnapshot:
        return await self._call_model(
            self._tools.disarm, {"device_id": device_id}, DeviceSnapshot
        )

    async def observe(self, device_id: str) -> DeviceSnapshot:
        return await self._call_model(
            self._tools.observe, {"device_id": device_id}, DeviceSnapshot
        )

    async def execute(self, command: HardwareCommand) -> CommandReceipt:
        return await self._call_model(
            self._tools.execute, command.model_dump(mode="json"), CommandReceipt
        )

    async def stop(self, device_id: str, reason: str) -> StopReceipt:
        return await self._call_model(
            self._tools.stop, {"device_id": device_id, "reason": reason}, StopReceipt
        )

    async def disconnect(self, device_id: str) -> None:
        await self._call(self._tools.disconnect, {"device_id": device_id})

    async def aclose(self) -> None:
        return None

    async def _call_model[ModelT: BaseModel](
        self, tool_name: str, arguments: dict[str, Any], model: type[ModelT]
    ) -> ModelT:
        payload = await self._call(tool_name, arguments)
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise MCPAdapterError(
                f"MCP tool {tool_name} returned an invalid Robo payload"
            ) from exc

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self._pool.call_tool(
                command=self._command,
                tool_name=tool_name,
                arguments=arguments,
                env=self._env,
                cwd=self._cwd,
                startup_timeout_sec=self._startup_timeout_sec,
                tool_timeout_sec=self._tool_timeout_sec,
            )
        except Exception as exc:
            raise MCPAdapterError(f"MCP tool {tool_name} failed: {exc}") from exc
        if result.structured is not None:
            return result.structured
        if result.text is None:
            raise MCPAdapterError(f"MCP tool {tool_name} returned no payload")
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise MCPAdapterError(
                f"MCP tool {tool_name} returned non-JSON text"
            ) from exc
        if not isinstance(payload, dict):
            raise MCPAdapterError(f"MCP tool {tool_name} returned a non-object payload")
        return payload


__all__ = ["MCPAdapterError", "MCPDeviceToolNames", "MCPStdioDeviceAdapter"]
