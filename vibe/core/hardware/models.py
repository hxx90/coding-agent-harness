from __future__ import annotations

from datetime import datetime
from enum import StrEnum, auto
import json
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

MAX_TRACE_EVENTS = 200
MAX_TRACE_PAYLOAD_BYTES = 16 * 1024


class DeviceTransport(StrEnum):
    SIMULATOR = auto()
    MCP_STDIO = auto()
    MCP_HTTP = auto()


class DeviceStatus(StrEnum):
    DISCOVERED = auto()
    CONNECTED = auto()
    ARMED = auto()
    STOPPED = auto()
    FAULTED = auto()
    UNKNOWN = auto()


class CommandStatus(StrEnum):
    COMPLETED = auto()
    FAILED = auto()
    UNKNOWN = auto()


class ActionCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    parameters_schema: dict[str, JsonValue] = Field(default_factory=dict)
    destructive: bool = True


class DeviceCapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    device_id: str
    adapter_id: str
    manufacturer: str
    model: str
    kind: str
    transport: DeviceTransport
    actions: list[ActionCapability]
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class DeviceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    status: DeviceStatus
    sequence: int = Field(ge=0)
    observed_at: datetime
    state: dict[str, JsonValue] = Field(default_factory=dict)
    faults: list[str] = Field(default_factory=list)


class DeviceLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(default_factory=lambda: uuid4().hex)
    device_id: str
    owner: str
    acquired_at: datetime
    expires_at: datetime


class HardwareCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(default_factory=lambda: uuid4().hex)
    device_id: str
    lease_id: str
    action: str
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class CommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    device_id: str
    status: CommandStatus
    started_at: datetime
    completed_at: datetime
    result: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None


class StopReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    acknowledged: bool
    observed_at: datetime
    message: str


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    device_id: str | None = Field(default=None, max_length=256)
    command_id: str | None = Field(default=None, max_length=128)
    payload: dict[str, JsonValue] = Field(default_factory=dict, max_length=32)

    @field_validator("payload")
    @classmethod
    def validate_payload_size(
        cls, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode()
        if len(encoded) > MAX_TRACE_PAYLOAD_BYTES:
            raise ValueError(
                f"trace payload exceeds {MAX_TRACE_PAYLOAD_BYTES} encoded bytes"
            )
        return payload


class RunTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    events: list[RunEvent] = Field(max_length=MAX_TRACE_EVENTS)


def json_value(value: Any) -> JsonValue:
    return value
