from __future__ import annotations

from typing import Protocol

from vibe.core.hardware.models import (
    CommandReceipt,
    DeviceCapabilityManifest,
    DeviceSnapshot,
    HardwareCommand,
    StopReceipt,
)


class DeviceAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    async def discover(self) -> list[DeviceCapabilityManifest]: ...

    async def connect(self, device_id: str) -> DeviceSnapshot: ...

    async def arm(self, device_id: str) -> DeviceSnapshot: ...

    async def disarm(self, device_id: str) -> DeviceSnapshot: ...

    async def observe(self, device_id: str) -> DeviceSnapshot: ...

    async def execute(self, command: HardwareCommand) -> CommandReceipt: ...

    async def stop(self, device_id: str, reason: str) -> StopReceipt: ...

    async def disconnect(self, device_id: str) -> None: ...

    async def aclose(self) -> None: ...


__all__ = ["DeviceAdapter"]
