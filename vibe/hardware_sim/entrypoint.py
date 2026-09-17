from __future__ import annotations

import os
from pathlib import Path
import sys

from vibe.hardware_sim.models import SimulatorSettings


def main() -> None:
    settings = SimulatorSettings.from_environment()
    _restart_with_mjpython_on_macos(settings)
    from vibe.hardware_sim.server import run_server

    run_server(settings)


def _restart_with_mjpython_on_macos(settings: SimulatorSettings) -> None:
    if sys.platform != "darwin" or settings.headless:
        return
    if os.getenv("MJPYTHON_BIN"):
        return
    mjpython = Path(sys.executable).with_name("mjpython")
    if not mjpython.is_file():
        raise SystemExit(
            "MuJoCo's mjpython launcher is missing; run `uv sync --extra simulator`"
        )
    os.execv(
        mjpython, [str(mjpython), "-m", "vibe.hardware_sim.entrypoint", *sys.argv[1:]]
    )


if __name__ == "__main__":
    main()
