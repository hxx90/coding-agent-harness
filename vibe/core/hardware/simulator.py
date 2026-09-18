from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

from pydantic import JsonValue

from vibe.core.hardware.models import (
    ActionCapability,
    CommandReceipt,
    CommandStatus,
    DeviceCapabilityManifest,
    DeviceSnapshot,
    DeviceStatus,
    DeviceTransport,
    HardwareCommand,
    StopReceipt,
    VerificationCapability,
    VerificationCheck,
    VerificationReport,
    VerificationStatus,
)

_ARM_DOF = 6


class SimulatorAdapterError(RuntimeError):
    pass


class DeterministicSimulatorAdapter:
    def __init__(self, *, adapter_id: str = "simulator") -> None:
        self._adapter_id = adapter_id
        self._sequence = 0
        self._connected = False
        self._armed = False
        self._state: dict[str, JsonValue] = {
            "mode": "discovered",
            "joints_rad": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "gripper": "open",
            "held_object": None,
            "folded_cloths": [],
        }

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    async def discover(self) -> list[DeviceCapabilityManifest]:
        return [
            DeviceCapabilityManifest(
                device_id="sim-arm-1",
                adapter_id=self.adapter_id,
                manufacturer="Robo",
                model="Robo Deterministic Arm",
                kind="robot_arm",
                transport=DeviceTransport.SIMULATOR,
                actions=[
                    ActionCapability(
                        name="home", description="Move to the deterministic home pose"
                    ),
                    ActionCapability(
                        name="move_joints",
                        description="Move six simulated joints in radians",
                        parameters_schema={
                            "joints_rad": {
                                "type": "array",
                                "minItems": 6,
                                "maxItems": 6,
                            }
                        },
                    ),
                    ActionCapability(
                        name="gripper_open", description="Open the simulated gripper"
                    ),
                    ActionCapability(
                        name="gripper_close", description="Close the simulated gripper"
                    ),
                    ActionCapability(
                        name="pick",
                        description="Pick a named simulated object",
                        parameters_schema={"object": {"type": "string"}},
                    ),
                    ActionCapability(
                        name="place", description="Place the currently held object"
                    ),
                    ActionCapability(
                        name="fold_cloth",
                        description="Fold a named cloth in the deterministic scene",
                        parameters_schema={"cloth": {"type": "string"}},
                    ),
                ],
                verifications=[
                    VerificationCapability(
                        name="holding_object",
                        description="Verify that the named object is held",
                        parameters_schema={"object": {"type": "string"}},
                        applicable_actions=["pick"],
                        parameter_bindings={"object": "object"},
                    ),
                    VerificationCapability(
                        name="cloth_folded",
                        description="Verify that the named cloth is folded",
                        parameters_schema={"cloth": {"type": "string"}},
                        applicable_actions=["fold_cloth"],
                        parameter_bindings={"cloth": "cloth"},
                    ),
                ],
                metadata={
                    "control_mode": "deterministic_semantic_actions",
                    "is_physical": False,
                    "dof": 6,
                },
            )
        ]

    async def connect(self, device_id: str) -> DeviceSnapshot:
        self._validate_device(device_id)
        self._connected = True
        self._armed = False
        self._state["mode"] = "idle"
        return self._snapshot()

    async def arm(self, device_id: str) -> DeviceSnapshot:
        self._validate_connected(device_id)
        self._armed = True
        self._state["mode"] = "armed"
        self._sequence += 1
        return self._snapshot()

    async def disarm(self, device_id: str) -> DeviceSnapshot:
        self._validate_connected(device_id)
        self._armed = False
        self._state["mode"] = "idle"
        self._sequence += 1
        return self._snapshot()

    async def observe(self, device_id: str) -> DeviceSnapshot:
        self._validate_connected(device_id)
        return self._snapshot()

    async def verify(
        self, device_id: str, criterion: str, parameters: dict[str, JsonValue]
    ) -> VerificationReport:
        self._validate_connected(device_id)
        match criterion:
            case "holding_object":
                expected = parameters.get("object")
                if not isinstance(expected, str) or not expected:
                    raise SimulatorAdapterError(
                        "holding_object verification requires a named object"
                    )
                observed = self._state["held_object"]
                passed = observed == expected
                check_name = "held_object_matches"
            case "cloth_folded":
                expected = parameters.get("cloth")
                if not isinstance(expected, str) or not expected:
                    raise SimulatorAdapterError(
                        "cloth_folded verification requires a named cloth"
                    )
                folded = self._state["folded_cloths"]
                passed = isinstance(folded, list) and expected in folded
                observed = passed
                check_name = "cloth_in_folded_set"
            case _:
                raise SimulatorAdapterError(
                    f"Unsupported simulator verification criterion: {criterion}"
                )
        status = VerificationStatus.PASSED if passed else VerificationStatus.FAILED
        return VerificationReport(
            device_id=device_id,
            criterion=criterion,
            status=status,
            observed_at=datetime.now(UTC),
            snapshot_sequence=self._sequence,
            checks=[
                VerificationCheck(
                    name=check_name,
                    status=status,
                    observed=observed,
                    expected=str(expected),
                )
            ],
            summary=(
                "The requested postcondition is satisfied."
                if passed
                else "The requested postcondition is not satisfied."
            ),
        )

    async def execute(self, command: HardwareCommand) -> CommandReceipt:
        self._validate_connected(command.device_id)
        started_at = datetime.now(UTC)
        result = self._apply_action(command.action, command.parameters)
        self._state["mode"] = "idle"
        self._sequence += 1
        return CommandReceipt(
            command_id=command.command_id,
            device_id=command.device_id,
            status=CommandStatus.COMPLETED,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            result=result,
        )

    async def stop(self, device_id: str, reason: str) -> StopReceipt:
        self._validate_device(device_id)
        self._armed = False
        self._state["mode"] = "stopped"
        self._sequence += 1
        return StopReceipt(
            device_id=device_id,
            acknowledged=True,
            observed_at=datetime.now(UTC),
            message=f"Simulator stopped: {reason}",
        )

    async def disconnect(self, device_id: str) -> None:
        self._validate_device(device_id)
        self._connected = False
        self._armed = False
        self._state["mode"] = "disconnected"

    async def aclose(self) -> None:
        self._connected = False
        self._armed = False

    def _apply_action(
        self, action: str, parameters: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        self._state["mode"] = "moving"
        match action:
            case "home":
                self._state["joints_rad"] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            case "move_joints":
                joints = parameters.get("joints_rad")
                if not isinstance(joints, list) or len(joints) != _ARM_DOF:
                    raise SimulatorAdapterError("move_joints requires six joints_rad")
                self._state["joints_rad"] = joints
            case "gripper_open":
                self._state["gripper"] = "open"
            case "gripper_close":
                self._state["gripper"] = "closed"
            case "pick":
                target = parameters.get("object")
                if not isinstance(target, str) or not target:
                    raise SimulatorAdapterError("pick requires a named object")
                self._state["held_object"] = target
                self._state["gripper"] = "closed"
            case "place":
                self._state["held_object"] = None
                self._state["gripper"] = "open"
            case "fold_cloth":
                cloth = parameters.get("cloth")
                if not isinstance(cloth, str) or not cloth:
                    raise SimulatorAdapterError("fold_cloth requires a named cloth")
                folded = self._state["folded_cloths"]
                if not isinstance(folded, list):
                    raise SimulatorAdapterError(
                        "simulator folded-cloth state is invalid"
                    )
                if cloth not in folded:
                    folded.append(cloth)
            case _:
                raise SimulatorAdapterError(f"Unsupported simulator action: {action}")
        return {"state": deepcopy(self._state)}

    def _snapshot(self) -> DeviceSnapshot:
        if self._state["mode"] == "stopped":
            status = DeviceStatus.STOPPED
        elif self._armed:
            status = DeviceStatus.ARMED
        elif self._connected:
            status = DeviceStatus.CONNECTED
        else:
            status = DeviceStatus.DISCOVERED
        return DeviceSnapshot(
            device_id="sim-arm-1",
            status=status,
            sequence=self._sequence,
            observed_at=datetime.now(UTC),
            state=deepcopy(self._state),
        )

    @staticmethod
    def _validate_device(device_id: str) -> None:
        if device_id != "sim-arm-1":
            raise SimulatorAdapterError(f"Unknown simulated device: {device_id}")

    def _validate_connected(self, device_id: str) -> None:
        self._validate_device(device_id)
        if not self._connected:
            raise SimulatorAdapterError("Simulated device is not connected")


__all__ = ["DeterministicSimulatorAdapter", "SimulatorAdapterError"]
