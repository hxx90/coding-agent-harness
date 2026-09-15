from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import time
import uuid

import pytest

from agent_harness.coding.types import CodingError
from agent_harness.robo.client import socket_path
from agent_harness.robo.demo import MANIFEST, demo, wait_job
from agent_harness.robo.programs import source_bundle


def publish(client, source, manifest=None, extra=None):
    spec = copy.deepcopy(manifest or MANIFEST)
    sources = {"control.py": source, **(extra or {})}
    spec["files"] = list(sources)
    return client.request("publish", manifest=spec, sources=sources)


def submit(client, version, **kwargs):
    return client.request(
        "submit",
        program_version=version["id"],
        request_id=uuid.uuid4().hex,
        parameters={"gain": 0.65},
        based_on=client.request("observe")["id"],
        **kwargs,
    )


def events(client, key):
    result = []
    cursor = 0
    while True:
        page = client.request("events", job_id=key, after=cursor)
        if not page["events"]:
            return result
        result += page["events"]
        cursor = page["cursor"]


def test_full_closed_loop_and_reconciliation(host):
    client, _, _ = host
    outcome = demo(client.root)
    assert outcome["passed"], outcome
    bad, good = outcome["trials"]
    assert bad["exit_code"] == 0 and bad["physical_verification"]["status"] == "failed"
    assert good["program_version"] != bad["program_version"]
    timeline = events(client, good["job_id"])
    assert all(
        e["job_id"] == good["job_id"]
        and e["program_version"] == good["program_version"]
        for e in timeline
    )
    assert any(
        e["type"] == "action_reconciled" and e["action"]["status"] == "confirmed"
        for e in timeline
    )
    observed = next(e["observation"] for e in timeline if e["type"] == "observation")
    assert observed["sampled_at"] <= observed["received_at"]
    assert observed["image"]["mime"] == "image/png"
    assert client.request("media", ref=observed["image"])["base64"].startswith("iVBOR")
    assert len({e["seq"] for e in timeline}) == len(timeline)


def test_source_dependency_isolation_reconnect_and_request_id(host, tmp_path):
    client, _, _ = host
    root = tmp_path / "source"
    root.mkdir()
    (root / "control.py").write_text(
        "robo.sleep(0.7)\nimport helper\nprint(helper.VALUE)\n"
    )
    (root / "helper.py").write_text('VALUE = "old dependency"\n')
    manifest = copy.deepcopy(MANIFEST)
    manifest["files"] = ["control.py", "helper.py"]
    (root / "program.json").write_text(json.dumps(manifest))
    version = client.request("publish", **source_bundle(root, root / "program.json"))
    args = {
        "program_version": version["id"],
        "request_id": "same-request",
        "parameters": {"gain": 0.65},
        "based_on": client.request("observe")["id"],
    }
    job = client.request("submit", **args)
    assert client.request("submit", **args)["id"] == job["id"]
    with pytest.raises(CodingError, match="different parameters"):
        client.request("submit", **{**args, "parameters": {"gain": 0.4}})
    (root / "control.py").write_text('raise RuntimeError("edited source")')
    (root / "helper.py").write_text('VALUE = "edited dependency"\n')
    client.request("disconnect", job_id=job["id"])
    result = wait_job(client, job["id"])
    assert result["exit_code"] == 0
    assert any(e.get("text") == "old dependency" for e in events(client, job["id"]))
    assert client.request("lookup", request_id="same-request")["id"] == job["id"]


def test_cancel_fences_gateway_and_confirms_device_stop(host):
    client, _, _ = host
    version = publish(client, "while True:\n    robo.observe()\n    robo.sleep(0.05)\n")
    job = submit(client, version)
    with pytest.raises(CodingError, match="owned"):
        submit(client, version)
    response = client.request("cancel", job_id=job["id"])
    assert response["status"] == "cancelling"
    final = wait_job(client, job["id"])
    assert final["status"] == "cancelled"
    assert final["stop_confirmation"]["status"] == "confirmed"
    assert final["stop_confirmation"]["moving"] is False
    timeline = events(client, job["id"])
    cancelled_at = next(e["seq"] for e in timeline if e["type"] == "cancel_requested")
    assert not any(
        e["type"] == "action_intent" and e["seq"] > cancelled_at for e in timeline
    )


def test_timeout_even_when_program_closes_pipes_and_lease_loss(host):
    client, _, _ = host
    spec = copy.deepcopy(MANIFEST)
    spec["limits"]["wall_seconds"] = 0.4
    version = publish(
        client,
        "import os\nimport time\nos.close(1)\nos.close(2)\ntime.sleep(10)\n",
        spec,
    )
    final = wait_job(client, submit(client, version)["id"])
    assert final["status"] == "timed_out"
    assert final["finished_at"] - final["created_at"] < 3
    version = publish(client, "robo.sleep(12)\n")
    job = submit(client, version, disconnect_policy="cancel")
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(socket_path(client.root)))
        connection.sendall(
            (
                json.dumps(
                    {
                        "protocol": 1,
                        "op": "events",
                        "args": {"job_id": job["id"], "after": 10**12, "wait": 10},
                    }
                )
                + "\n"
            ).encode()
        )
    time.sleep(5.4)  # Abrupt client loss: no event subscription renews the lease.
    final = wait_job(client, job["id"])
    assert final["status"] == "cancelled"


def test_host_crash_never_replays_motion(host):
    client, process, start = host
    source = 'o = robo.observe()\nrobo.move(-2, 0, based_on=o["id"], command_id="once")\nrobo.sleep(10)\n'
    job = submit(client, publish(client, source))
    for update in client.subscribe(job["id"]):
        if any(e["type"] == "action_result" for e in update["events"]):
            break
    before = client.request("observe")["state"]
    worker_pid = client.request("status", job_id=job["id"])["pid"]
    process.kill()
    process.wait(timeout=3)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        state = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(worker_pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not state or state.startswith("Z"):
            break
        time.sleep(0.02)
    else:
        os.kill(worker_pid, 9)
        pytest.fail("Worker survived host death")
    client, _ = start()
    recovered = client.request("status", job_id=job["id"])
    assert recovered["status"] == "interrupted"
    assert recovered["physical_verification"]["status"] == "unknown"
    assert client.request("observe")["state"] == before
    assert (
        len([e for e in events(client, job["id"]) if e["type"] == "action_intent"]) == 1
    )


@pytest.mark.parametrize(
    "source, expected",
    [
        (
            'o=robo.observe()\nrobo.move(5,0,based_on=o["id"],command_id="too-far")',
            "dx must be finite",
        ),
        (
            'o=robo.observe()\nrobo.sleep(2.1)\nrobo.move(1,0,based_on=o["id"],command_id="stale")',
            "stale_observation",
        ),
        ('print("x" * 65500)\n', "request exceeds"),
        ("data = bytearray(200 * 1024 * 1024)\nrobo.sleep(2)", "memory budget"),
    ],
)
def test_resource_and_freshness_limits(host, source, expected):
    client, _, _ = host
    manifest = copy.deepcopy(MANIFEST)
    manifest["limits"]["memory_mb"] = 80
    final = wait_job(client, submit(client, publish(client, source, manifest))["id"])
    assert final["status"] == "failed"
    assert expected in json.dumps(events(client, final["id"]))


def test_sandbox_blocks_direct_access_and_external_imports(host, tmp_path):
    client, _, _ = host
    marker = tmp_path / "private.txt"
    marker.write_text("private")
    source = f"""import os
import socket
for operation in [lambda: open({str(marker)!r}).read(), lambda: open("new.txt", "w"),
                  lambda: socket.create_connection(("127.0.0.1", 8501)), lambda: os.fork()]:
    try:
        operation()
        raise RuntimeError("sandbox bypass")
    except PermissionError:
        print("blocked")
try:
    import httpx
    raise RuntimeError("ambient dependency")
except ImportError:
    print("no site packages")
"""
    final = wait_job(client, submit(client, publish(client, source))["id"])
    assert final["exit_code"] == 0, events(client, final["id"])
    assert [e.get("text") for e in events(client, final["id"])].count("blocked") == 4


def test_unknown_command_requires_reconciliation_and_deduplicates(host):
    client, _, _ = host
    source = """scene=robo.observe()
args=dict(dx=-2,dy=0,based_on=scene["id"],command_id="same",lose_ack=True)
assert robo.move(**args)["status"] == "unknown"
assert robo.move(**args)["status"] == "unknown"
try:
    robo.move(-2,0,based_on=scene["id"],command_id="new")
    raise RuntimeError("unreconciled action allowed")
except Exception as e:
    assert "reconciliation_required" in str(e)
assert robo.query("same")["status"] == "confirmed"
assert robo.move(**args)["status"] == "confirmed"
print("reconciled without repeating")
"""
    before = client.request("observe")["state"]["x_mm"]
    final = wait_job(client, submit(client, publish(client, source))["id"])
    assert final["exit_code"] == 0
    assert client.request("observe")["state"]["x_mm"] == before - 2


def test_grants_policy_and_protection_are_host_owned(host):
    client, _, _ = host
    manifest = copy.deepcopy(MANIFEST)
    manifest["devices"] = {"sim-camera": ["observe"]}
    version = publish(
        client,
        'o=robo.observe()\nrobo.move(1,0,based_on=o["id"],command_id="no-grant")',
        manifest,
    )
    final = wait_job(client, submit(client, version)["id"])
    assert "No sim-stage.move grant" in json.dumps(events(client, final["id"]))
    manifest["limits"]["max_step_mm"] = 100
    with pytest.raises(CodingError, match="max_step_mm"):
        submit(client, publish(client, "pass", manifest))
    client.request("protect")
    with pytest.raises(CodingError, match="protection latched"):
        submit(client, version)
    assert client.request("health")["mhs"]["sdk_version"] is None


def test_worker_backpressure_cannot_block_timeout(host):
    client, _, _ = host
    manifest = copy.deepcopy(MANIFEST)
    manifest["limits"]["wall_seconds"] = 0.5
    source = """import sys
sys.__stdout__.write('{"op":"observe","args":{}}\\n' * 20)
sys.__stdout__.flush()
robo.sleep(10)
"""
    final = wait_job(client, submit(client, publish(client, source, manifest))["id"])
    assert final["status"] == "timed_out"
    assert final["finished_at"] - final["created_at"] < 3


def test_agent_observation_does_not_invalidate_local_camera_sample(host):
    client, _, _ = host
    source = 'o=robo.observe()\nprint("sampled")\nrobo.sleep(0.3)\nrobo.move(-1,0,based_on=o["id"],command_id="valid-revision")\n'
    job = submit(client, publish(client, source))
    for update in client.subscribe(job["id"]):
        if any(e.get("text") == "sampled" for e in update["events"]):
            client.request("observe")
            break
    assert wait_job(client, job["id"])["exit_code"] == 0


def test_program_integrity_and_mhs_fail_closed(host):
    client, _, _ = host
    version = publish(client, 'print("frozen")')
    artifact = client.root / "programs" / (version["id"] + ".zip")
    artifact.chmod(0o600)
    import zipfile

    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("control.py", 'print("tampered")')
    final = wait_job(client, submit(client, version)["id"])
    assert final["status"] == "failed" and final["exit_code"] is None
    assert "integrity check" in final["error"]
    from agent_harness.robo.runtime import Runtime

    with pytest.raises(CodingError, match="Application-only"):
        Runtime(client.root / "mhs", backend="mhs")


def test_handover_reconciles_unknown_action_before_new_job(host):
    client, _, _ = host
    source = 'o=robo.observe()\nrobo.move(-1,0,based_on=o["id"],command_id="lost",lose_ack=True)\n'
    first = wait_job(client, submit(client, publish(client, source))["id"])
    assert "uncertain_action" not in first
    assert (
        client.request("action", job_id=first["id"], command_id="lost")["status"]
        == "confirmed"
    )
    second = wait_job(client, submit(client, publish(client, source))["id"])
    reconciled = next(
        e for e in events(client, first["id"]) if e["type"] == "action_reconciled"
    )
    dispatched = next(
        e for e in events(client, second["id"]) if e["type"] == "action_intent"
    )
    assert reconciled["seq"] < dispatched["seq"]
