"""Native PickPlaceCan workcell contract and evaluator-only task grading."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
import math
from typing import Any

from vibe.core.hardware import (
    DeviceSnapshot,
    VerificationCheck,
    VerificationReport,
    VerificationStatus,
)

STABLE_CYCLES = 20
_MAX_POSITION_DRIFT_M = 0.002
_MIN_QUATERNION_DOT = math.cos(math.radians(2) / 2)


def pickplace_goal() -> dict[str, Any]:
    """Public fixed fixture specification, never derived from live object state.

    Robosuite 1.5.2 PickPlaceCan defaults: bin2=(.1,.28,.8),
    table_full_size=(.39,.49,.82), Can destination quadrant index 3.
    The physical floor is .02m above the bin body's origin; walls reach .9m.
    """
    return {
        "version": "pickplace-can-workcell-v1",
        "source": "public-fixed-workcell-specification",
        "frame": "world-z-up",
        "units": "m",
        "object": "red soda can",
        "destination": "can compartment (bin 3)",
        "center_xy": [0.1975, 0.4025],
        "bounds_xy": [[0.1, 0.295], [0.28, 0.525]],
        "floor_z": 0.82,
        "wall_top_z": 0.9,
        "verification": "place_object",
    }


def pickplace_released(environment: Any) -> bool:
    """Private scorer: official bin predicate plus explicit no-grasp check."""
    target = environment.objects[environment.object_id]
    return bool(environment._check_success()) and not environment._check_grasp(
        gripper=environment.robots[0].gripper, object_geoms=target
    )


class PlacementStability:
    """Bounded evaluator-only pose window, robust to mesh contact velocity jitter."""

    def __init__(self) -> None:
        self._poses: deque[tuple[float, ...]] = deque(maxlen=STABLE_CYCLES)

    def reset(self) -> None:
        self._poses.clear()

    @property
    def cycles(self) -> int:
        return len(self._poses)

    @staticmethod
    def _pose(environment: Any) -> tuple[float, ...]:
        target = environment.objects[environment.object_id]
        return tuple(
            float(v) for v in environment.sim.data.get_joint_qpos(target.joints[0])
        )

    def observe(self, environment: Any) -> None:
        if not pickplace_released(environment):
            self.reset()
            return
        self._poses.append(self._pose(environment))

    def settled(self, environment: Any) -> bool:
        if self.cycles < STABLE_CYCLES or not pickplace_released(environment):
            return False
        poses = [*self._poses, self._pose(environment)]
        # Check every pair: bounded peak-to-peak drift, not accumulated noise.
        for index, first in enumerate(poses):
            for second in poses[index + 1 :]:
                if math.dist(first[:3], second[:3]) > _MAX_POSITION_DRIFT_M:
                    return False
                dot = abs(
                    sum(a * b for a, b in zip(first[3:], second[3:], strict=True))
                )
                if dot < _MIN_QUATERNION_DOT:
                    return False
        return True


def verify_pickplace(
    environment: Any, snapshot: DeviceSnapshot, stability: PlacementStability
) -> VerificationReport:
    # Observations never include these object poses, velocities or contact IDs.
    released = pickplace_released(environment)
    stable = stability.settled(environment)
    evidence_ids = [item.evidence_id for item in snapshot.evidence]
    passed = released and stable
    return VerificationReport(
        device_id=snapshot.device_id,
        criterion="place_object",
        status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
        observed_at=datetime.now(UTC),
        snapshot_sequence=snapshot.sequence,
        checks=[
            VerificationCheck(
                name="official_pickplace_released",
                status=VerificationStatus.PASSED
                if released
                else VerificationStatus.FAILED,
                observed=released,
                expected="Can in assigned bin, gripper retreated and not grasping",
                evidence_ids=evidence_ids,
            ),
            VerificationCheck(
                name="stable_placement",
                status=VerificationStatus.PASSED
                if stable
                else VerificationStatus.FAILED,
                observed=stability.cycles,
                expected="20 consecutive cycles: released in bin, peak-to-peak position drift <=0.002 m and rotation drift <=2 degrees",
                evidence_ids=evidence_ids,
            ),
        ],
        summary="Can released and stably placed in its assigned bin."
        if passed
        else "PickPlace postconditions not satisfied.",
        evidence=snapshot.evidence,
        evidence_ids=evidence_ids,
    )
