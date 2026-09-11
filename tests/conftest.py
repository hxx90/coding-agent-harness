from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures"


@pytest.fixture
def workspace_factory(tmp_path):
    def make(name: str, validation_command=None) -> Path:
        source = FIXTURE_ROOT / name
        destination = tmp_path / name
        shutil.copytree(source, destination)
        config_path = destination / ".agent-harness.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["validation_command"] = validation_command or [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
        ]
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return destination

    return make


@pytest.fixture
def runtime_dir(tmp_path) -> Path:
    path = tmp_path / "runtime"
    path.mkdir()
    return path

