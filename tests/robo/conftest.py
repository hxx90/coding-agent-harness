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
    if sys.platform not in {"darwin", "linux"}:
        pytest.skip("Robo requires macOS or Linux isolation")
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
        # Cold imports on the shared workspace can exceed five seconds while
        # simulator/vision services start. This fixture is not a startup SLA.
        deadline = time.monotonic() + 15
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

    try:
        client, process = start()
        yield client, process, start
    finally:
        # Also reap children when setup fails before reaching yield.
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=15)
