from __future__ import annotations

import pytest

from vibe.hardware_sim.models import SimulatorSettings


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("false", False)])
def test_simulator_settings_parse_headless_environment(
    value: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROBO_SIM_HEADLESS", value)

    assert SimulatorSettings.from_environment().headless is expected


def test_simulator_settings_parse_recording_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROBO_RECORDING_DIR", str(tmp_path))

    assert SimulatorSettings.from_environment().recording_dir == tmp_path
