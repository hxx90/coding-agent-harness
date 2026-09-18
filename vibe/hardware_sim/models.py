from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class SimulatorSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    headless: bool = False
    evidence_dir: Path | None = None

    @classmethod
    def from_environment(cls) -> SimulatorSettings:
        return cls.model_validate({
            "headless": os.environ.get("ROBO_SIM_HEADLESS", False),
            "evidence_dir": os.environ.get("ROBO_EVIDENCE_DIR") or None,
        })


__all__ = ["SimulatorSettings"]
