from __future__ import annotations

from vibe.hardware_sim.models import SimulatorSettings


def main() -> None:
    settings = SimulatorSettings.from_environment()
    from vibe.hardware_sim.server import run_server

    run_server(settings)


if __name__ == "__main__":
    main()
