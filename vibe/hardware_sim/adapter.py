from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import math
from pathlib import Path
import struct
from typing import Any
from uuid import uuid4
import zlib

from pydantic import JsonValue

from vibe.core.hardware import (
    ActionCapability,
    CommandReceipt,
    CommandStatus,
    DeviceCapabilityManifest,
    DeviceSnapshot,
    DeviceStatus,
    DeviceTransport,
    HardwareCommand,
    ObservationEvidence,
    ObservationModality,
    StopReceipt,
    VerificationCapability,
    VerificationCheck,
    VerificationReport,
    VerificationStatus,
)

MUJOCO_PANDA_DEVICE_ID = "mujoco-panda-1"
_CARTESIAN_DIMENSIONS = 3
_IMAGE_DIMENSIONS = 3
_RGB_CHANNELS = 3
_MAX_ACTION_STEPS = 200
_LIFT_HEIGHT_THRESHOLD_M = 0.04


class RobosuiteSimulatorError(RuntimeError):
    pass


class RobosuiteSimulatorAdapter:
    def __init__(
        self,
        *,
        adapter_id: str = "robosuite",
        show_viewer: bool = True,
        control_frequency: int = 20,
        evidence_dir: Path | None = None,
    ) -> None:
        self._adapter_id = adapter_id
        self._show_viewer = show_viewer
        self._control_frequency = control_frequency
        self._evidence_dir = evidence_dir
        self._environment: Any | None = None
        self._observation: dict[str, Any] = {}
        self._home_cube_position: list[float] = []
        self._connected = False
        self._armed = False
        self._stopped = False
        self._sequence = 0

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    async def discover(self) -> list[DeviceCapabilityManifest]:
        return [
            DeviceCapabilityManifest(
                device_id=MUJOCO_PANDA_DEVICE_ID,
                adapter_id=self.adapter_id,
                manufacturer="Franka Emika",
                model="Panda (Robosuite Lift)",
                kind="robot_arm",
                transport=DeviceTransport.SIMULATOR,
                actions=[
                    ActionCapability(
                        name="home", description="Reset the Panda and cube scene"
                    ),
                    ActionCapability(
                        name="move_cartesian",
                        description="Move the end effector in normalized XYZ control space",
                        parameters_schema={
                            "delta_xyz": {
                                "type": "array",
                                "minItems": 3,
                                "maxItems": 3,
                                "items": {
                                    "type": "number",
                                    "minimum": -1,
                                    "maximum": 1,
                                },
                            },
                            "steps": {"type": "integer", "minimum": 1, "maximum": 200},
                        },
                    ),
                    ActionCapability(
                        name="gripper_open", description="Open the Panda gripper"
                    ),
                    ActionCapability(
                        name="gripper_close", description="Close the Panda gripper"
                    ),
                ],
                verifications=[
                    VerificationCapability(
                        name="lift_object",
                        description=(
                            "Verify from observed contact and scene state that the "
                            "cube is grasped and lifted"
                        ),
                        applicable_actions=[
                            "home",
                            "move_cartesian",
                            "gripper_open",
                            "gripper_close",
                        ],
                    )
                ],
                metadata={
                    "environment": "Lift",
                    "control_mode": "osc_pose",
                    "dof": 7,
                    "has_viewer": self._show_viewer,
                    "is_physical": False,
                },
            )
        ]

    async def connect(self, device_id: str) -> DeviceSnapshot:
        self._validate_device(device_id)
        self._ensure_environment()
        self._connected = True
        self._armed = False
        self._stopped = False
        self._sequence += 1
        self._render()
        return self._snapshot()

    async def arm(self, device_id: str) -> DeviceSnapshot:
        self._validate_connected(device_id)
        self._armed = True
        self._stopped = False
        self._sequence += 1
        return self._snapshot()

    async def disarm(self, device_id: str) -> DeviceSnapshot:
        self._validate_connected(device_id)
        self._armed = False
        self._sequence += 1
        return self._snapshot()

    async def observe(self, device_id: str) -> DeviceSnapshot:
        self._validate_connected(device_id)
        return self._snapshot(capture_evidence=True)

    async def verify(
        self, device_id: str, criterion: str, parameters: dict[str, JsonValue]
    ) -> VerificationReport:
        self._validate_connected(device_id)
        if criterion != "lift_object":
            raise RobosuiteSimulatorError(
                f"Unsupported MuJoCo verification criterion: {criterion}"
            )
        del parameters
        snapshot = self._snapshot(capture_evidence=True)
        state = snapshot.state
        evidence_ids = [item.evidence_id for item in snapshot.evidence]
        grasped = state["grasped"] is True
        cube_height = state["cube_height_above_table_m"]
        lifted = (
            isinstance(cube_height, int | float)
            and cube_height > _LIFT_HEIGHT_THRESHOLD_M
        )
        status = (
            VerificationStatus.PASSED
            if grasped and lifted
            else VerificationStatus.FAILED
        )
        return VerificationReport(
            device_id=device_id,
            criterion=criterion,
            status=status,
            observed_at=datetime.now(UTC),
            snapshot_sequence=self._sequence,
            checks=[
                VerificationCheck(
                    name="object_grasped",
                    status=(
                        VerificationStatus.PASSED
                        if grasped
                        else VerificationStatus.FAILED
                    ),
                    observed=grasped,
                    expected="true",
                    evidence_ids=evidence_ids,
                ),
                VerificationCheck(
                    name="object_lifted",
                    status=(
                        VerificationStatus.PASSED
                        if lifted
                        else VerificationStatus.FAILED
                    ),
                    observed=lifted,
                    expected=(
                        f"cube at least {_LIFT_HEIGHT_THRESHOLD_M:.2f} m above the table"
                    ),
                    evidence_ids=evidence_ids,
                ),
            ],
            summary=(
                "The cube is grasped and lifted."
                if status is VerificationStatus.PASSED
                else "The lift task postconditions are not satisfied."
            ),
            evidence=snapshot.evidence,
            evidence_ids=evidence_ids,
        )

    async def execute(self, command: HardwareCommand) -> CommandReceipt:
        self._validate_connected(command.device_id)
        if not self._armed:
            raise RobosuiteSimulatorError("MuJoCo Panda is not armed")
        started_at = datetime.now(UTC)
        result = await self._apply_action(command.action, command.parameters)
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
        self._stopped = True
        self._sequence += 1
        if self._environment is not None:
            self._step_once(self._zero_action())
        return StopReceipt(
            device_id=device_id,
            acknowledged=True,
            observed_at=datetime.now(UTC),
            message=f"MuJoCo simulator stopped: {reason}",
        )

    async def disconnect(self, device_id: str) -> None:
        self._validate_device(device_id)
        self._close_environment()

    async def aclose(self) -> None:
        self._close_environment()

    @property
    def _mode(self) -> str:
        if self._stopped:
            return "stopped"
        if self._armed:
            return "armed"
        return "idle"

    def _ensure_environment(self) -> None:
        if self._environment is not None:
            return
        try:
            import robosuite
        except ModuleNotFoundError as exc:
            raise RobosuiteSimulatorError(
                "Robosuite is not installed; run `uv sync --extra simulator`"
            ) from exc
        environment = robosuite.make(
            env_name="Lift",
            robots="Panda",
            has_renderer=self._show_viewer,
            has_offscreen_renderer=self._evidence_dir is not None,
            use_camera_obs=self._evidence_dir is not None,
            camera_names="agentview",
            camera_heights=384,
            camera_widths=384,
            ignore_done=True,
            reward_shaping=False,
            control_freq=self._control_frequency,
            hard_reset=False,
        )
        self._environment = environment
        self._observation = environment.reset()
        self._home_cube_position = self._float_values("cube_pos")

    async def _apply_action(
        self, action_name: str, parameters: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        match action_name:
            case "home":
                self._observation = self._require_environment().reset()
                self._home_cube_position = self._float_values("cube_pos")
                self._render()
            case "move_cartesian":
                delta = parameters.get("delta_xyz")
                if not isinstance(delta, list) or len(delta) != _CARTESIAN_DIMENSIONS:
                    raise RobosuiteSimulatorError(
                        "move_cartesian requires three delta_xyz values"
                    )
                values = [self._normalized_number(value) for value in delta]
                steps = self._step_count(parameters.get("steps", 20))
                action = self._zero_action()
                action[:3] = values
                await self._step(action, steps=steps)
            case "gripper_open":
                action = self._zero_action()
                action[-1] = -1.0
                await self._step(action, steps=20)
            case "gripper_close":
                action = self._zero_action()
                action[-1] = 1.0
                await self._step(action, steps=20)
            case _:
                raise RobosuiteSimulatorError(
                    f"Unsupported MuJoCo simulator action: {action_name}"
                )
        return {"state": self._state(self._mode)}

    async def _step(self, action: Any, *, steps: int) -> None:
        for _ in range(steps):
            if self._stopped:
                raise asyncio.CancelledError
            self._step_once(action)
            delay = 1 / self._control_frequency if self._show_viewer else 0
            await asyncio.sleep(delay)

    def _step_once(self, action: Any) -> None:
        self._observation, *_ = self._require_environment().step(action)
        self._render()

    def _render(self) -> None:
        if self._show_viewer and self._environment is not None:
            self._environment.render()

    def _zero_action(self) -> Any:
        lower_bound, _ = self._require_environment().action_spec
        return lower_bound * 0.0

    def _require_environment(self) -> Any:
        if self._environment is None:
            raise RobosuiteSimulatorError("MuJoCo Panda is not connected")
        return self._environment

    def _snapshot(self, *, capture_evidence: bool = False) -> DeviceSnapshot:
        status = DeviceStatus.CONNECTED
        if self._stopped:
            status = DeviceStatus.STOPPED
        elif self._armed:
            status = DeviceStatus.ARMED
        evidence = self._capture_rgb_evidence() if capture_evidence else []
        return DeviceSnapshot(
            device_id=MUJOCO_PANDA_DEVICE_ID,
            status=status,
            sequence=self._sequence,
            observed_at=datetime.now(UTC),
            state=self._state(self._mode),
            evidence=evidence,
        )

    def _capture_rgb_evidence(self) -> list[ObservationEvidence]:
        if self._evidence_dir is None:
            return []
        image = self._observation.get("agentview_image")
        if image is None or getattr(image, "ndim", None) != _IMAGE_DIMENSIONS:
            return []
        height, width, channels = image.shape
        if channels != _RGB_CHANNELS:
            return []
        captured_at = datetime.now(UTC)
        evidence_id = uuid4().hex
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        path = (self._evidence_dir / f"{evidence_id}.png").resolve()
        path.write_bytes(_encode_rgb_png(image))
        return [
            ObservationEvidence(
                evidence_id=evidence_id,
                source_id="agentview",
                modality=ObservationModality.RGB,
                captured_at=captured_at,
                sequence=self._sequence,
                mime_type="image/png",
                uri=path.as_uri(),
                width=width,
                height=height,
            )
        ]

    def _state(self, mode: str) -> dict[str, Any]:
        end_effector_position = self._float_values("robot0_eef_pos")
        cube_position = self._float_values("cube_pos")
        return {
            "mode": mode,
            "environment": "Lift",
            "viewer": self._show_viewer,
            "joint_positions": self._float_values("robot0_joint_pos"),
            "end_effector_position": end_effector_position,
            "gripper_positions": self._float_values("robot0_gripper_qpos"),
            "cube_position": cube_position,
            "grasped": self._is_grasped(),
            "cube_height_above_table_m": self._cube_height_above_table(cube_position),
            "gripper_to_cube_distance_m": self._distance(
                end_effector_position, cube_position
            ),
            "cube_displacement_from_home_m": self._distance(
                self._home_cube_position, cube_position
            ),
        }

    def _float_values(self, key: str) -> list[float]:
        return [float(value) for value in self._observation.get(key, [])]

    def _is_grasped(self) -> bool:
        environment = self._require_environment()
        return bool(
            environment._check_grasp(
                gripper=environment.robots[0].gripper, object_geoms=environment.cube
            )
        )

    def _cube_height_above_table(self, cube_position: list[float]) -> float:
        if len(cube_position) != _CARTESIAN_DIMENSIONS:
            return 0.0
        environment = self._require_environment()
        table_height = float(environment.model.mujoco_arena.table_offset[2])
        return cube_position[2] - table_height

    @staticmethod
    def _distance(first: list[float], second: list[float]) -> float:
        if len(first) != len(second) or not first:
            return 0.0
        return math.dist(first, second)

    def _close_environment(self) -> None:
        if self._environment is not None:
            self._environment.close()
        self._environment = None
        self._observation = {}
        self._home_cube_position = []
        self._connected = False
        self._armed = False
        self._stopped = False

    @staticmethod
    def _normalized_number(value: JsonValue) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise RobosuiteSimulatorError("delta_xyz values must be numbers")
        number = float(value)
        if not -1 <= number <= 1:
            raise RobosuiteSimulatorError("delta_xyz values must be between -1 and 1")
        return number

    @staticmethod
    def _step_count(value: JsonValue) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= _MAX_ACTION_STEPS
        ):
            raise RobosuiteSimulatorError("steps must be an integer from 1 to 200")
        return value

    @staticmethod
    def _validate_device(device_id: str) -> None:
        if device_id != MUJOCO_PANDA_DEVICE_ID:
            raise RobosuiteSimulatorError(f"Unknown simulated device: {device_id}")

    def _validate_connected(self, device_id: str) -> None:
        self._validate_device(device_id)
        if not self._connected:
            raise RobosuiteSimulatorError("MuJoCo Panda is not connected")


__all__ = [
    "MUJOCO_PANDA_DEVICE_ID",
    "RobosuiteSimulatorAdapter",
    "RobosuiteSimulatorError",
]


def _encode_rgb_png(image: Any) -> bytes:
    height, width, channels = image.shape
    if channels != _RGB_CHANNELS:
        raise RobosuiteSimulatorError("RGB evidence must have exactly three channels")
    rows = b"".join(b"\x00" + image[row].tobytes() for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join([
        b"\x89PNG\r\n\x1a\n",
        chunk(b"IHDR", header),
        chunk(b"IDAT", zlib.compress(rows)),
        chunk(b"IEND", b""),
    ])
