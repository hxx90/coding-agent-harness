from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import UTC, datetime
import json
import logging
import math
from pathlib import Path
import struct
from typing import Any, Literal
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
from vibe.hardware_sim._pickplace import (
    PlacementStability,
    pickplace_goal,
    verify_pickplace,
)
from vibe.hardware_sim._target_motion import run_target_motion
from vibe.hardware_sim._viewer import ViewerProcess
from vibe.hardware_sim.models import TargetMoveParameters, TargetMoveReport

MUJOCO_PANDA_DEVICE_ID = "mujoco-panda-1"
_CARTESIAN_DIMENSIONS = 3
_IMAGE_DIMENSIONS = 3
_RGB_CHANNELS = 3
_MAX_ACTION_STEPS = 200
_LIFT_HEIGHT_THRESHOLD_M = 0.04
_STACK_STABLE_CYCLES = 20
_STACK_LINEAR_SPEED_M_S = 0.01
_STACK_ANGULAR_SPEED_RAD_S = 0.1
_CAPTURE_CAMERA = "sideview"

logger = logging.getLogger(__name__)


class RobosuiteSimulatorError(RuntimeError):
    pass


class RobosuiteSimulatorAdapter:
    def __init__(
        self,
        *,
        adapter_id: str = "robosuite",
        show_viewer: bool = True,
        control_frequency: int = 20,
        observation_mode: Literal["privileged", "rgbd"] = "privileged",
        geometry_library: Literal["off", "h1-v8-v9"] = "off",
        seed: int | None = None,
        evidence_dir: Path | None = None,
        recording_dir: Path | None = None,
        max_control_cycles: int | None = None,
        task: Literal["Lift", "Stack", "PickPlace"] = "Lift",
    ) -> None:
        if task not in {"Lift", "Stack", "PickPlace"}:
            raise ValueError("Only Lift, Stack and PickPlace are supported")
        if task != "Lift" and geometry_library != "off":
            raise ValueError(f"{task} pilot requires H1 off")
        if task == "PickPlace" and observation_mode != "rgbd":
            raise ValueError("PickPlace requires RGB-D observation mode")
        self._task = task
        self._camera = "agentview" if task == "PickPlace" else _CAPTURE_CAMERA
        self._pickplace_stability = PlacementStability()
        self._stack_stable_cycles = 0
        self._support_perception: dict[str, Any] = {}
        if geometry_library != "off" and observation_mode != "rgbd":
            raise ValueError("H1 requires RGB-D observation mode")
        self._observation_mode = observation_mode
        self._geometry_library = geometry_library
        self._seed = seed
        self._perception_sequence = -1
        self._perception: dict[str, Any] = {}
        self._adapter_id = adapter_id
        self._show_viewer = show_viewer
        self._control_frequency = control_frequency
        self._evidence_dir = evidence_dir
        self._recording_dir = recording_dir
        self._environment: Any | None = None
        self._video_writer: Any | None = None
        self._recording_path: Path | None = None
        self._viewer: ViewerProcess | None = None
        self._viewer_failed = False
        self._observation: dict[str, Any] = {}
        self._home_cube_position: list[float] = []
        self._connected = False
        self._armed = False
        self._stopped = False
        self._sequence = 0
        if max_control_cycles is not None and (
            isinstance(max_control_cycles, bool) or max_control_cycles <= 0
        ):
            raise ValueError("Control cycle limit must be positive")
        self._max_control_cycles = max_control_cycles
        self._control_cycles_used = 0
        self._cleanup_step = False
        self._last_target_move: TargetMoveReport | None = None

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    async def discover(self) -> list[DeviceCapabilityManifest]:
        return [
            DeviceCapabilityManifest(
                device_id=MUJOCO_PANDA_DEVICE_ID,
                adapter_id=self.adapter_id,
                manufacturer="Franka Emika",
                model=f"Panda (Robosuite {self._task})",
                kind="robot_arm",
                transport=DeviceTransport.SIMULATOR,
                actions=[
                    ActionCapability(
                        name="home", description="Reset the Panda and task scene"
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
                        name="move_to",
                        description=(
                            "Track a fixed world-frame XYZ target in meters with fresh "
                            "proprioceptive feedback every 20Hz control cycle. Defaults: "
                            "72 total cycles, 30 seconds, 20 consecutive cycles within "
                            "tolerance_xyz=[0.006,0.006,0.008]m. No H1 required. "
                            "Return a complete CommandReceipt dict, identical for "
                            "move_to(...) and act('move_to', ...). Read "
                            "receipt['result']['motion']['reached']; there is no "
                            "top-level receipt['motion']. result.motion contains "
                            "reached, reason, error_xyz, "
                            "error_max_m, cycles, settled_cycles and elapsed_seconds. "
                            "Unreached returns FAILED and aborts the policy before "
                            "later stages. reached is control success, not lift success."
                        ),
                        parameters_schema=TargetMoveParameters.model_json_schema()[
                            "properties"
                        ],
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
                        name=self._verification_criterion,
                        description=(
                            "Verify the can is released in its assigned bin, robot "
                            "retreated and object settled for 20 control cycles."
                            if self._task == "PickPlace"
                            else "Verify red cube stacked on green cube, released, with "
                            "official Stack success and low object velocities for "
                            "20 consecutive control cycles (1 simulation second)."
                            if self._task == "Stack"
                            else "Verify from observed contact and scene state that the "
                            "cube is grasped and lifted"
                        ),
                        applicable_actions=[
                            "home",
                            "move_cartesian",
                            "move_to",
                            "gripper_open",
                            "gripper_close",
                        ],
                    )
                ],
                metadata={
                    "environment": self._task,
                    "default_verification": self._verification_criterion,
                    "control_mode": "osc_pose",
                    "dof": 7,
                    "has_viewer": self._show_viewer,
                    "records_video": self._recording_dir is not None,
                    "is_physical": False,
                    "observation_mode": self._observation_mode,
                    "geometry_library": self._geometry_library,
                    "geometry_notes": self._geometry_notes(),
                    "control_cycle_limit": self._max_control_cycles,
                    "control_cycle_budget": "All motion/gripper cycles share this session limit; stop cleanup is separate. home/connect/arm do not refill it.",
                    "perception_contract": "RGB-D: state.perception contains red-rgbd-v1 points (max 128), centroid, ok/count, metric world-z-up. Check ok before motion. end_effector_position is robot proprioception. delta_xyz is normalized per-step control (0.05 m scale), not a meter target."
                    + (
                        " Stack adds state.support_perception: green-rgbd-v1 points and centroid of the green support cube, same camera/time/frame/units. Both clouds use RGB colors and aligned depth only. Task: release the red cube on top of the green cube, retreat and allow settling; verify stack_objects."
                        if self._task == "Stack"
                        else " PickPlace uses native PickPlaceCan. state.perception is the red can (not a cube). state.task_goal gives the public fixed destination bin and wall height from the workcell specification, NOT sensed object state. Camera is agentview. Move the can to its assigned bin, release, retreat, settle and verify place_object. Red-cloud centroid is visible surface geometry, not object center or a guaranteed grasp pose."
                        if self._task == "PickPlace"
                        else ""
                    ),
                },
            )
        ]

    @property
    def _verification_criterion(self) -> str:
        return {
            "Lift": "lift_object",
            "Stack": "stack_objects",
            "PickPlace": "place_object",
        }[self._task]

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
        if criterion == "place_object" and self._task == "PickPlace":
            return verify_pickplace(
                self._require_environment(),
                self._snapshot(capture_evidence=True),
                self._pickplace_stability,
            )
        if criterion == "stack_objects" and self._task == "Stack":
            return self._verify_stack()
        if criterion != "lift_object" or self._task != "Lift":
            raise RobosuiteSimulatorError(
                f"Unsupported MuJoCo verification criterion: {criterion}"
            )
        del parameters
        snapshot = self._snapshot(capture_evidence=True)
        evidence_ids = [item.evidence_id for item in snapshot.evidence]
        grasped = self._is_grasped()
        cube_height = self._cube_height_above_table(self._float_values("cube_pos"))
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

    def _stack_settled(self) -> bool:
        """Evaluator only: never expose object state to the actor's perception."""
        environment = self._require_environment()
        if not environment._check_success():
            return False
        for cube in (environment.cubeA, environment.cubeB):
            velocity = environment.sim.data.get_joint_qvel(cube.joints[0])
            if math.dist(velocity[:3], (0, 0, 0)) >= _STACK_LINEAR_SPEED_M_S:
                return False
            if math.dist(velocity[3:], (0, 0, 0)) >= _STACK_ANGULAR_SPEED_RAD_S:
                return False
        return True

    def _verify_stack(self) -> VerificationReport:
        snapshot = self._snapshot(capture_evidence=True)
        evidence_ids = [item.evidence_id for item in snapshot.evidence]
        official = bool(self._require_environment()._check_success())
        stable = (
            self._stack_stable_cycles >= _STACK_STABLE_CYCLES and self._stack_settled()
        )
        return VerificationReport(
            device_id=MUJOCO_PANDA_DEVICE_ID,
            criterion="stack_objects",
            status=VerificationStatus.PASSED
            if official and stable
            else VerificationStatus.FAILED,
            observed_at=datetime.now(UTC),
            snapshot_sequence=self._sequence,
            checks=[
                VerificationCheck(
                    name="official_stack_released",
                    status=VerificationStatus.PASSED
                    if official
                    else VerificationStatus.FAILED,
                    observed=official,
                    expected="true",
                    evidence_ids=evidence_ids,
                ),
                VerificationCheck(
                    name="stable_stack",
                    status=VerificationStatus.PASSED
                    if stable
                    else VerificationStatus.FAILED,
                    observed=self._stack_stable_cycles,
                    expected="20 consecutive cycles: official Stack success, linear speed <0.01 m/s and angular speed <0.1 rad/s for both cubes",
                    evidence_ids=evidence_ids,
                ),
            ],
            summary="Red cube released and stably stacked on green."
            if official and stable
            else "Stack postconditions not satisfied.",
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
        failed = (
            command.action == "move_to"
            and self._last_target_move is not None
            and not self._last_target_move.reached
        )
        return CommandReceipt(
            command_id=command.command_id,
            device_id=command.device_id,
            status=CommandStatus.FAILED if failed else CommandStatus.COMPLETED,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            result=result,
            error=(
                f"move_to did not reach target: {self._last_target_move.reason}"
                if failed and self._last_target_move is not None
                else None
            ),
        )

    async def stop(self, device_id: str, reason: str) -> StopReceipt:
        self._validate_device(device_id)
        self._armed = False
        if not self._stopped:
            self._stopped = True
            self._sequence += 1
            if self._environment is not None:
                self._cleanup_step = True
                try:
                    self._step_once(self._zero_action())
                finally:
                    self._cleanup_step = False
        self._finish_recording()
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
        camera_enabled = self._observation_mode == "rgbd" or any((
            self._evidence_dir,
            self._recording_dir,
        ))
        environment = robosuite.make(
            env_name="PickPlaceCan" if self._task == "PickPlace" else self._task,
            robots="Panda",
            # The native Viewer renders a state mirror in its own process.
            # Robosuite only needs an offscreen context when evidence or MP4
            # capture is enabled in this simulator process.
            has_renderer=False,
            has_offscreen_renderer=camera_enabled,
            use_camera_obs=camera_enabled,
            camera_names=self._camera,
            camera_depths=self._observation_mode == "rgbd",
            seed=self._seed,
            camera_heights=384,
            camera_widths=384,
            ignore_done=True,
            reward_shaping=False,
            control_freq=self._control_frequency,
            hard_reset=False,
        )
        self._environment = environment
        self._observation = environment.reset()
        self._stack_stable_cycles = 0
        self._pickplace_stability.reset()
        self._home_cube_position = self._float_values("cube_pos")

    async def _apply_action(
        self, action_name: str, parameters: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        match action_name:
            case "move_to":
                report = await self._move_to(
                    TargetMoveParameters.model_validate(parameters)
                )
                return {"motion": report.model_dump(mode="json")}
            case "home":
                self._observation = self._require_environment().reset()
                self._stack_stable_cycles = 0
                self._pickplace_stability.reset()
                self._perception_sequence = -1
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
        if not self._cleanup_step and self._cycles_remaining() == 0:
            raise RobosuiteSimulatorError("Session control cycle budget exhausted")
        self._observation, *_ = self._require_environment().step(action)
        if self._task == "Stack":
            self._stack_stable_cycles = (
                self._stack_stable_cycles + 1 if self._stack_settled() else 0
            )
        if self._task == "PickPlace":
            self._pickplace_stability.observe(self._require_environment())
        if not self._cleanup_step:
            self._control_cycles_used += 1
        self._perception_sequence = -1
        self._render()

    def _cycles_remaining(self) -> int | None:
        if self._max_control_cycles is None:
            return None
        return max(0, self._max_control_cycles - self._control_cycles_used)

    def _end_effector_position(self) -> tuple[float, float, float]:
        values = self._float_values("robot0_eef_pos")
        if len(values) != _CARTESIAN_DIMENSIONS or not all(map(math.isfinite, values)):
            raise RobosuiteSimulatorError("Invalid end-effector proprioception")
        return (values[0], values[1], values[2])

    def _world_delta_action(self, error: tuple[float, float, float]) -> Any:
        import numpy as np

        controller = (
            self
            ._require_environment()
            .robots[0]
            .composite_controller.part_controllers["right"]
        )
        if controller.input_type != "delta":
            raise RobosuiteSimulatorError("move_to requires the delta OSC controller")
        delta = np.asarray(error)
        if controller.input_ref_frame == "base":
            delta = controller.origin_ori.T @ delta
        elif controller.input_ref_frame != "world":
            raise RobosuiteSimulatorError("Unsupported OSC reference frame")
        action = self._zero_action()
        action[:3] = np.clip(delta / 0.05, -1.0, 1.0)
        return action

    async def _move_to(self, request: TargetMoveParameters) -> TargetMoveReport:
        with ExitStack() as stack:
            path = None
            trace = None
            if self._evidence_dir is not None:
                self._evidence_dir.mkdir(parents=True, exist_ok=True)
                path = self._evidence_dir / f"motion-{uuid4().hex}.jsonl"
                trace = stack.enter_context(path.open("w"))

            async def advance(error: tuple[float, float, float]) -> None:
                action = self._world_delta_action(error)
                self._step_once(action)
                if trace is not None:
                    trace.write(
                        json.dumps({
                            "session_cycle": self._control_cycles_used,
                            "target_xyz": request.target_xyz,
                            "position_xyz": self._end_effector_position(),
                            "delta_xyz": [float(v) for v in action[:3]],
                        })
                        + "\n"
                    )
                    trace.flush()
                await asyncio.sleep(
                    1 / self._control_frequency if self._show_viewer else 0
                )

            def finish(report: TargetMoveReport) -> None:
                self._last_target_move = report
                if not report.reached:
                    # Fail closed even if a direct adapter caller ignores the receipt.
                    self._armed = False

            return await run_target_motion(
                request,
                position=self._end_effector_position,
                advance=advance,
                cycles_used=lambda: self._control_cycles_used,
                cycles_remaining=self._cycles_remaining,
                stopped=lambda: self._stopped,
                finish=finish,
                trace_uri=path.resolve().as_uri() if path is not None else None,
            )

    def _render(self) -> None:
        if self._environment is None:
            return
        self._publish_viewer_state()
        image = self._camera_image()
        if image is None:
            return
        self._record_frame(image)

    def _publish_viewer_state(self) -> None:
        if not self._show_viewer or self._viewer_failed:
            return
        if self._viewer is None:
            self._viewer = ViewerProcess()
        try:
            simulation = self._require_environment().sim
            self._viewer.publish(simulation.model, simulation.data)
        except Exception:
            logger.warning("Robo simulator Viewer failed", exc_info=True)
            try:
                self._viewer.close()
            except Exception:
                logger.warning("Robo simulator Viewer cleanup failed", exc_info=True)
            self._viewer = None
            self._viewer_failed = True

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
        image = self._camera_image()
        if image is None:
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
                source_id=self._camera,
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
        state = {
            "mode": mode,
            "environment": self._task,
            "viewer": self._viewer is not None and self._viewer.is_running,
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
            "recording_uri": (
                self._recording_path.as_uri()
                if self._recording_path is not None
                else None
            ),
            "control_cycles_used": self._control_cycles_used,
            "control_cycles_remaining": self._cycles_remaining(),
            "last_target_move": (
                self._last_target_move.model_dump(mode="json")
                if self._last_target_move is not None
                else None
            ),
        }

        if self._observation_mode == "rgbd":
            for key in (
                "cube_position",
                "grasped",
                "cube_height_above_table_m",
                "gripper_to_cube_distance_m",
                "cube_displacement_from_home_m",
            ):
                state.pop(key)
            state["perception"] = self._camera_perception()
            if self._task == "Stack":
                state["support_perception"] = self._support_perception
        if self._task == "PickPlace":
            state["task_goal"] = pickplace_goal()
        return state

    def _geometry_notes(self) -> str | None:
        if self._geometry_library == "off":
            return None
        from vibe.core.hardware._geometry import h1_description

        return h1_description()

    def _camera_perception(self) -> dict[str, Any]:
        if self._perception_sequence == self._sequence:
            return self._perception
        import numpy as np

        from vibe.hardware_sim._perception import (
            green_point_cloud,
            metric_camera,
            red_point_cloud,
        )

        simulation = self._require_environment().sim
        rgb = self._camera_image()
        if rgb is None:
            raise RobosuiteSimulatorError("RGB-D camera returned no RGB frame")
        raw_depth = self._observation[f"{self._camera}_depth"][::-1]
        depth, intrinsic, pose = metric_camera(simulation, self._camera, raw_depth)
        result = red_point_cloud(rgb, depth, intrinsic, pose)
        result.update(
            sequence=self._sequence,
            observed_at=datetime.now(UTC).isoformat(),
            intrinsics=intrinsic.tolist(),
            camera_to_world=pose.tolist(),
            image_origin="top-left",
            depth_convention="optical-z-meters",
        )
        if self._task == "Stack":
            self._support_perception = green_point_cloud(rgb, depth, intrinsic, pose)
            self._support_perception.update({
                key: result[key]
                for key in (
                    "sequence",
                    "observed_at",
                    "intrinsics",
                    "camera_to_world",
                    "image_origin",
                    "depth_convention",
                )
            })
        if self._evidence_dir is not None:
            self._evidence_dir.mkdir(parents=True, exist_ok=True)
            path = self._evidence_dir / f"rgbd-{self._sequence}-{uuid4().hex}.npz"
            np.savez_compressed(
                path, rgb=rgb, depth=depth, intrinsics=intrinsic, camera_to_world=pose
            )
            result["rgbd_uri"] = path.resolve().as_uri()
            if self._task == "Stack":
                self._support_perception["rgbd_uri"] = result["rgbd_uri"]
        self._perception = result
        self._perception_sequence = self._sequence
        return result

    def _camera_image(self) -> Any | None:
        image = self._observation.get(f"{self._camera}_image")
        if image is None or getattr(image, "ndim", None) != _IMAGE_DIMENSIONS:
            return None
        if image.shape[2] != _RGB_CHANNELS:
            return None
        return image[::-1]

    def _record_frame(self, image: Any) -> None:
        if self._recording_dir is None:
            return
        import cv2

        cv2_module: Any = cv2
        height, width, _ = image.shape
        if self._video_writer is None:
            self._recording_dir.mkdir(parents=True, exist_ok=True)
            recording_id = uuid4().hex
            self._recording_path = (
                self._recording_dir / f"mujoco-panda-{recording_id}.mp4"
            ).resolve()
            self._video_writer = cv2.VideoWriter(
                str(self._recording_path),
                cv2_module.VideoWriter_fourcc(*"mp4v"),
                float(self._control_frequency),
                (width, height),
            )
            if not self._video_writer.isOpened():
                self._video_writer.release()
                self._video_writer = None
                self._recording_path = None
                raise RobosuiteSimulatorError("Could not open MuJoCo MP4 recorder")
        self._video_writer.write(image[..., ::-1])

    def _finish_recording(self) -> None:
        if self._video_writer is None:
            return
        self._video_writer.release()
        self._video_writer = None

    def _float_values(self, key: str) -> list[float]:
        return [float(value) for value in self._observation.get(key, [])]

    def _is_grasped(self) -> bool:
        environment = self._require_environment()
        return bool(
            environment._check_grasp(
                gripper=environment.robots[0].gripper,
                object_geoms=environment.cubeA
                if self._task == "Stack"
                else environment.objects[environment.object_id]
                if self._task == "PickPlace"
                else environment.cube,
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
        self._finish_recording()
        if self._environment is not None:
            self._environment.close()
        if self._viewer is not None:
            try:
                self._viewer.close()
            except Exception:
                logger.warning("Robo simulator Viewer cleanup failed", exc_info=True)
            self._viewer = None
        self._viewer_failed = False
        self._environment = None
        self._perception_sequence = -1
        self._perception = {}
        self._support_perception = {}
        self._pickplace_stability.reset()
        self._stack_stable_cycles = 0
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
