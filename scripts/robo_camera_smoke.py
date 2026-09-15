"""Opt-in real camera acceptance: start a camera host first, then run this script.

Actual media stays inside the private runtime home. Printed reports contain
device/job metadata and pixel statistics, never image bytes.
"""

from __future__ import annotations

import argparse
import base64
import json
import time
import uuid
from pathlib import Path

from agent_harness.coding.media import MediaStore, tool_content
from agent_harness.robo.client import Client
from agent_harness.robo.demo import wait_job
from agent_harness.robo.programs import source_bundle


def smoke(root: Path) -> dict:
    client = Client(root)
    device = client.request("devices")
    assert device["backend"] == "camera" and device["simulated"] is False
    example = Path(__file__).resolve().parents[1] / "examples/robo/camera"
    workspace = root / "camera-workspaces" / uuid.uuid4().hex[:8]
    workspace.mkdir(parents=True, mode=0o700)
    manifest_path = workspace / "program.json"
    manifest_path.write_text((example / "program.json").read_text())
    source_path = workspace / "observe.py"
    source = (example / "observe.py").read_text()
    trials = []
    preview = None
    for revision in (1, 2):
        source_path.write_text(source.replace("version = 1", f"version = {revision}"))
        program = client.request("publish", **source_bundle(workspace, manifest_path))
        scene = client.request("observe")
        request_id = "camera-smoke-" + uuid.uuid4().hex
        job = client.request(
            "submit",
            program_version=program["id"],
            based_on=scene["id"],
            request_id=request_id,
            parameters={"frames": 5, "interval": 0.3},
        )
        # File edits after publishing cannot affect this running version.
        source_path.write_text('raise RuntimeError("edited workspace must not run")\n')
        client.request("disconnect", job_id=job["id"])
        reconnect = Client(root)
        assert reconnect.request("lookup", request_id=request_id)["id"] == job["id"]
        final = wait_job(reconnect, job["id"])
        assert final["status"] == "exited" and final["exit_code"] == 0, final
        assert final["simulated"] is False
        verification = final["physical_verification"]
        assert verification["status"] == "unknown"
        assert verification["capture_verification"]["status"] == "succeeded"
        events = reconnect.request("events", job_id=job["id"])["events"]
        observations = [e["observation"] for e in events if e["type"] == "observation"]
        samples = [
            json.loads(e["text"])
            for e in events
            if e["type"] == "log" and e["text"].startswith("{")
        ]
        assert len(samples) == 5 and all(s["version"] == revision for s in samples)
        assert len({o["capture_seq"] for o in observations}) == len(observations)
        assert all(
            o["simulated"] is False
            and o["device_uid"] == device["devices"][0]["uid"]
            and o["sampled_at"] <= o["received_at"]
            for o in observations
        )
        assert all(
            e["job_id"] == job["id"] and e["program_version"] == program["id"]
            for e in events
        )
        preview = observations[-1]
        trials.append(
            {
                "job_id": job["id"],
                "program_version": program["id"],
                "exit_code": final["exit_code"],
                "status": final["status"],
                "samples": samples,
                "physical_verification": verification,
                "reconnected": True,
                "version_isolated": True,
            }
        )
    assert trials[0]["program_version"] != trials[1]["program_version"]
    source_path.write_text("while True:\n    robo.observe()\n    robo.sleep(0.1)\n")
    program = client.request("publish", **source_bundle(workspace, manifest_path))
    job = client.request(
        "submit",
        program_version=program["id"],
        based_on=client.request("observe")["id"],
        request_id="camera-cancel-" + uuid.uuid4().hex,
        parameters={"frames": 5, "interval": 0.3},
    )
    for update in client.subscribe(job["id"]):
        if any(e["type"] == "observation" for e in update["events"]):
            break
    client.request("cancel", job_id=job["id"])
    final = wait_job(client, job["id"])
    assert (
        final["status"] == "cancelled"
        and final["stop_confirmation"]["status"] == "confirmed"
    )
    # Cancel revoked this program; the host's shared observation service remains usable.
    client.request("observe")
    assert preview is not None
    data = base64.b64decode(client.request("media", ref=preview["image"])["base64"])
    path = root / ("camera-preview-" + uuid.uuid4().hex[:8] + ".png")
    path.write_bytes(data)
    path.chmod(0o600)
    # Exercise real pixels through the existing provider serialization without a network call.
    media = MediaStore(root / "media")
    messages = [
        {
            "role": "tool",
            "tool_call_id": "camera-test",
            "content": tool_content({"id": preview["id"]}, [preview["image"]]),
        }
    ]
    wire = media.provider_messages(messages)
    assert "data:image/png;base64," in json.dumps(wire)
    return {
        "passed": True,
        "tested_at": time.time(),
        "devices": device,
        "trials": trials,
        "cancellation": {
            "job_id": final["id"],
            "status": final["status"],
            "stop_confirmation": final["stop_confirmation"],
        },
        "preview": str(path),
        "preview_metadata": {k: v for k, v in preview.items() if k != "image"},
        "provider_image_serialization": "passed locally; no model request sent",
        "mhs_integrated": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            smoke(args.home.expanduser().resolve()), ensure_ascii=False, indent=2
        )
    )
