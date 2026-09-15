from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from agent_harness.coding.types import CodingError
from agent_harness.robo.client import Client


@pytest.fixture
def host(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("Robo's first OS sandbox is macOS; Linux is explicitly unsupported")
    root = tmp_path / "host"
    processes = []

    def start():
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_harness.robo.cli",
                "--home",
                str(root),
                "host",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "ROBO_HOME": str(root)},
        )
        processes.append(process)
        deadline = time.monotonic() + 5
        client = Client(root)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(process.communicate())
            try:
                client.request("health")
                return client, process
            except CodingError:
                time.sleep(0.03)
        raise AssertionError("Host did not start")

    client, process = start()
    yield client, process, start
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=15)
