from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Coordinate = Annotated[float, Field(ge=-2, le=2, allow_inf_nan=False)]
Tolerance = Annotated[float, Field(ge=0.0001, le=0.05, allow_inf_nan=False)]
MotionReason = Literal[
    "reached", "step_limit", "time_limit", "cycle_budget", "cancelled", "error"
]


class TargetMoveParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_xyz: tuple[Coordinate, Coordinate, Coordinate]
    tolerance_xyz: tuple[Tolerance, Tolerance, Tolerance] = (0.006, 0.006, 0.008)
    max_steps: int = Field(default=72, ge=1, le=400, strict=True)
    timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    settle_steps: int = Field(default=20, ge=1, le=20, strict=True)


class TargetMoveReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reached: bool
    reason: MotionReason
    target_xyz: tuple[float, float, float]
    position_xyz: tuple[float, float, float]
    error_xyz: tuple[float, float, float]
    error_max_m: float
    cycles: int
    settled_cycles: int
    elapsed_seconds: float
    trace_uri: str | None = None


class SimulatorSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    headless: bool = False
    task: Literal["Lift", "Stack", "PickPlace"] = "Lift"
    observation_mode: Literal["privileged", "rgbd"] = "privileged"
    geometry_library: Literal["off", "h1-v8-v9"] = "off"
    seed: int | None = None
    max_control_cycles: int | None = Field(default=None, ge=1, le=16000)
    evidence_dir: Path | None = None
    recording_dir: Path | None = None

    @classmethod
    def from_environment(cls) -> SimulatorSettings:
        return cls.model_validate({
            "task": os.environ.get("ROBO_SIM_TASK", "Lift"),
            "observation_mode": os.environ.get(
                "ROBO_SIM_OBSERVATION_MODE", "privileged"
            ),
            "geometry_library": os.environ.get("ROBO_SIM_GEOMETRY_LIBRARY", "off"),
            "seed": os.environ.get("ROBO_SIM_SEED") or None,
            "max_control_cycles": os.environ.get("ROBO_SIM_MAX_CONTROL_CYCLES") or None,
            "headless": os.environ.get("ROBO_SIM_HEADLESS", False),
            "evidence_dir": os.environ.get("ROBO_EVIDENCE_DIR") or None,
            "recording_dir": os.environ.get("ROBO_RECORDING_DIR") or None,
        })


__all__ = ["SimulatorSettings", "TargetMoveParameters", "TargetMoveReport"]
