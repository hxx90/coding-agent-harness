from __future__ import annotations

from datetime import datetime
from enum import StrEnum, auto
import json
from typing import Any
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

MAX_TRACE_EVENTS = 200
MAX_TRACE_PAYLOAD_BYTES = 16 * 1024
MAX_OBSERVATION_EVIDENCE = 4
MAX_VERIFICATION_CHECKS = 32


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


class VerificationStatus(StrEnum):
    PASSED = auto()
    FAILED = auto()
    INCONCLUSIVE = auto()


class ObservationModality(StrEnum):
    RGB = auto()
    DEPTH = auto()
    POINT_CLOUD = auto()
    THERMAL = auto()


class ObservationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(default_factory=lambda: uuid4().hex, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    modality: ObservationModality
    captured_at: datetime
    sequence: int = Field(ge=0)
    mime_type: str = Field(min_length=1, max_length=128)
    uri: str = Field(min_length=1, max_length=4096)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)


class ActionCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    parameters_schema: dict[str, JsonValue] = Field(default_factory=dict)
    destructive: bool = True


class VerificationCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1024)
    parameters_schema: dict[str, JsonValue] = Field(default_factory=dict, max_length=64)
    applicable_actions: list[str] = Field(default_factory=list, max_length=32)
    parameter_bindings: dict[str, str] = Field(default_factory=dict, max_length=64)


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
    verifications: list[VerificationCapability] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_verification_actions(self) -> DeviceCapabilityManifest:
        actions = {action.name: action for action in self.actions}
        unknown = {
            action
            for verification in self.verifications
            for action in verification.applicable_actions
            if action not in actions
        }
        if unknown:
            raise ValueError(
                "verification capabilities reference unknown actions: "
                + ", ".join(sorted(unknown))
            )
        for verification in self.verifications:
            unknown_parameters = set(verification.parameter_bindings).difference(
                verification.parameters_schema
            )
            if unknown_parameters:
                raise ValueError(
                    f"verification {verification.name} binds unknown parameters: "
                    + ", ".join(sorted(unknown_parameters))
                )
            for action_name in verification.applicable_actions:
                action_parameters = actions[action_name].parameters_schema
                unknown_sources = set(
                    verification.parameter_bindings.values()
                ).difference(action_parameters)
                if unknown_sources:
                    raise ValueError(
                        f"verification {verification.name} reads unknown parameters "
                        f"from {action_name}: " + ", ".join(sorted(unknown_sources))
                    )
        return self


class DeviceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    status: DeviceStatus
    sequence: int = Field(ge=0)
    observed_at: datetime
    state: dict[str, JsonValue] = Field(default_factory=dict)
    faults: list[str] = Field(default_factory=list)
    evidence: list[ObservationEvidence] = Field(
        default_factory=list, max_length=MAX_OBSERVATION_EVIDENCE
    )


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


class VerificationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    status: VerificationStatus
    observed: JsonValue = None
    expected: str = Field(max_length=2048)
    evidence_ids: list[str] = Field(
        default_factory=list, max_length=MAX_OBSERVATION_EVIDENCE
    )


class VerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verification_id: str = Field(default_factory=lambda: uuid4().hex, max_length=128)
    device_id: str = Field(min_length=1, max_length=256)
    criterion: str = Field(min_length=1, max_length=128)
    status: VerificationStatus
    observed_at: datetime
    snapshot_sequence: int = Field(ge=0)
    checks: list[VerificationCheck] = Field(
        min_length=1, max_length=MAX_VERIFICATION_CHECKS
    )
    summary: str = Field(max_length=4096)
    evidence: list[ObservationEvidence] = Field(
        default_factory=list, max_length=MAX_OBSERVATION_EVIDENCE
    )
    evidence_ids: list[str] = Field(
        default_factory=list, max_length=MAX_OBSERVATION_EVIDENCE
    )


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
