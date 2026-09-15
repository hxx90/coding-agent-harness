"""Deterministic local visual alignment with one failed candidate and a repair."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from agent_harness.coding.types import Json

from .client import Client
from .programs import source_bundle

MANIFEST: Json = {
    "entrypoint": "control.py",
    "files": ["control.py"],
    "dependencies": {},
    "devices": {"sim-camera": ["observe"], "sim-stage": ["move", "query"]},
    "parameters": {"gain": {"min": 0.1, "max": 1}},
    "limits": {"wall_seconds": 15, "max_actions": 80, "max_events": 350},
}

BAD = """# First candidate deliberately uses the wrong correction direction.
scene = robo.observe()
x, y = robo.centroid(scene)
robo.move(2 if x > 32 else -2, 0, based_on=scene["id"], command_id="wrong-direction")
print("Code finished, but this does not prove alignment.")
"""

GOOD = """# Local visual feedback: raw pixels -> centroid -> bounded correction.
# `robo` is the SDK injected by the frozen execution worker.
import math

stable = 0
for step in range(70):
    scene = robo.observe()
    x, y = robo.centroid(scene)
    error = math.hypot(x - 32, y - 32)
    print("step", step, "pixel error", error)
    if error <= 0.5:
        stable += 1
        if stable >= 5:
            break
    else:
        stable = 0
        gain = robo.parameters["gain"]
        dx = max(-4, min(4, (32 - x) * gain))
        dy = max(-4, min(4, (32 - y) * gain))
        command = "correction-" + str(step)
        result = robo.move(dx, dy, based_on=scene["id"], command_id=command,
                           lose_ack=(step == 0))
        if result["status"] == "unknown":
            result = robo.query(command)
            if result["status"] != "confirmed":
                raise RuntimeError("Motion remains unknown; stopping for inspection")
    robo.sleep(0.03)
else:
    raise RuntimeError("Local controller did not converge")
print("Local loop complete; the host will independently observe the result.")
"""


def wait_job(client: Client, job_id: str) -> Json:
    final: Json = {}
    for update in client.subscribe(job_id):
        final = update["job"]
    return final


def demo(root: Path) -> Json:
    client = Client(root)
    discovered = client.request("devices")
    directory = root / "demo-workspace" / uuid.uuid4().hex[:8]
    directory.mkdir(parents=True)
    (directory / "program.json").write_text(json.dumps(MANIFEST, indent=2))
    results = []
    for label, source in (("wrong-direction", BAD), ("repaired", GOOD)):
        (directory / "control.py").write_text(source)
        version = client.request(
            "publish", **source_bundle(directory, directory / "program.json")
        )
        scene = client.request("observe")
        request_id = "demo-" + uuid.uuid4().hex
        submitted = client.request(
            "submit",
            program_version=version["id"],
            based_on=scene["id"],
            request_id=request_id,
            parameters={"gain": 0.65},
            disconnect_policy="continue_bounded",
        )
        # Reconnect from a new client after dropping the original call/connection.
        reconnect = Client(root)
        assert (
            reconnect.request("lookup", request_id=request_id)["id"] == submitted["id"]
        )
        job = wait_job(reconnect, submitted["id"])
        results.append(
            {
                "trial": label,
                "program_version": version["id"],
                "job_id": job["id"],
                "execution": job["status"],
                "exit_code": job["exit_code"],
                "physical_verification": job["physical_verification"],
            }
        )
    return {
        "simulated": True,
        "mhs_integrated": False,
        "devices": discovered["devices"],
        "workspace": str(directory),
        "trials": results,
        "finished_at": time.time(),
        "passed": results[0]["exit_code"] == 0
        and results[0]["physical_verification"]["status"] == "failed"
        and results[1]["physical_verification"]["status"] == "succeeded",
    }
