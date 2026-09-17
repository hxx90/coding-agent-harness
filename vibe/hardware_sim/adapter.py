from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

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
    StopReceipt,
)

MUJOCO_PANDA_DEVICE_ID = "mujoco-panda-1"
_CARTESIAN_DIMENSIONS = 3
_MAX_ACTION_STEPS = 200


class RobosuiteSimulatorError(RuntimeError):
    pass


class RobosuiteSimulatorAdapter:
    def __init__(
        self,
        *,
        adapter_id: str = "robosuite",
        show_viewer: bool = True,
        control_frequency: int = 20,
    ) -> None:
        self._adapter_id = adapter_id
        self._show_viewer = show_viewer
        self._control_frequency = control_frequency
        self._environment: Any | None = None
        self._observation: dict[str, Any] = {}
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
        return self._snapshot()

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
            has_offscreen_renderer=False,
            use_camera_obs=False,
            ignore_done=True,
            reward_shaping=True,
            control_freq=self._control_frequency,
            hard_reset=False,
        )
        self._environment = environment
        self._observation = environment.reset()

    async def _apply_action(
        self, action_name: str, parameters: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        match action_name:
            case "home":
                self._observation = self._require_environment().reset()
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

    def _snapshot(self) -> DeviceSnapshot:
        status = DeviceStatus.CONNECTED
        if self._stopped:
            status = DeviceStatus.STOPPED
        elif self._armed:
            status = DeviceStatus.ARMED
        return DeviceSnapshot(
            device_id=MUJOCO_PANDA_DEVICE_ID,
            status=status,
            sequence=self._sequence,
            observed_at=datetime.now(UTC),
            state=self._state(self._mode),
        )

    def _state(self, mode: str) -> dict[str, JsonValue]:
        return {
            "mode": mode,
            "environment": "Lift",
            "viewer": self._show_viewer,
            "joint_positions": self._values("robot0_joint_pos"),
            "end_effector_position": self._values("robot0_eef_pos"),
            "gripper_positions": self._values("robot0_gripper_qpos"),
            "cube_position": self._values("cube_pos"),
        }

    def _values(self, key: str) -> list[JsonValue]:
        values = self._observation.get(key)
        if values is None:
            return []
        return [float(value) for value in values]

    def _close_environment(self) -> None:
        if self._environment is not None:
            self._environment.close()
        self._environment = None
        self._observation = {}
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
