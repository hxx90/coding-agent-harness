"""Project bounded cleanup without granting it actor/tool success semantics."""

from __future__ import annotations

from pydantic import ValidationError

from vibe.app_server.models import (
    HardwareCleanupDevice,
    HardwareCleanupResult,
    HardwareStopReceiptOutput,
    HardwareVerificationOutput,
)


def project_hardware_cleanup(raw: dict[str, object]) -> HardwareCleanupResult:
    result = HardwareCleanupResult()
    if isinstance(error := raw.get("error"), str):
        result.error = error
    devices = raw.get("devices")
    if not isinstance(devices, dict):
        result.error = result.error or "invalid_cleanup_result"
        return result
    for device_id, item in devices.items():
        if not isinstance(device_id, str) or not isinstance(item, dict):
            result.error = result.error or "invalid_cleanup_device"
            continue
        device = HardwareCleanupDevice(device_id=device_id)
        for operation in ("verify", "stop", "release"):
            value = item.get(operation)
            if isinstance(value, dict) and isinstance(value.get("error"), str):
                device.errors[operation] = value["error"]
                continue
            try:
                if operation == "verify" and value is not None:
                    device.verification = HardwareVerificationOutput.model_validate(
                        value
                    )
                    if device.verification.device_id not in {None, device_id}:
                        device.verification = None
                        device.errors[operation] = "device_mismatch"
                elif operation == "stop" and value is not None:
                    device.stop = HardwareStopReceiptOutput.model_validate(value)
                    if device.stop.device_id != device_id:
                        device.stop = None
                        device.errors[operation] = "device_mismatch"
                elif operation == "release":
                    if value is True:
                        device.release = "released"
                    elif value == "no_lease":
                        device.release = "no_lease"
            except ValidationError:
                device.errors[operation] = "invalid_cleanup_receipt"
        result.devices.append(device)
    return result
