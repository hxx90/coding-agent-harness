from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from vibe.hardware_sim import entrypoint
from vibe.hardware_sim.models import SimulatorSettings


def test_visible_server_runs_directly_without_mjpython_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimulatorSettings(headless=False)
    received: list[SimulatorSettings] = []
    monkeypatch.setattr(
        entrypoint.SimulatorSettings, "from_environment", lambda: settings
    )
    monkeypatch.setitem(
        sys.modules,
        "vibe.hardware_sim.server",
        SimpleNamespace(run_server=received.append),
    )

    entrypoint.main()

    assert received == [settings]
