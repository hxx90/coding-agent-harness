from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict


class SimulatorSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    headless: bool = False

    @classmethod
    def from_environment(cls) -> SimulatorSettings:
        return cls.model_validate({
            "headless": os.environ.get("ROBO_SIM_HEADLESS", False)
        })


__all__ = ["SimulatorSettings"]
