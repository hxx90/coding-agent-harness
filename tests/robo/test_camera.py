"""Deterministic protocol fixtures; real hardware evidence is recorded separately."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

import pytest

from agent_harness.coding.types import CodingError
from agent_harness.robo.camera import CameraStream
from agent_harness.robo.runtime import TERMINAL, Runtime


@pytest.fixture
def capture_fixture(tmp_path, monkeypatch):
    from agent_harness.robo import camera

    binary = tmp_path / "capture-test-fixture"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import base64, json, os, sys, threading, time
threading.Thread(target=lambda: (sys.stdin.buffer.read(1), os._exit(0)), daemon=True).start()
mode = sys.argv[-1]
if mode == "denied":
    print(json.dumps({"error": "camera_permission_denied", "message": "fixture denied"}), flush=True)
    sys.exit(1)
for seq in range(1, 10000):
    now = time.time()
    value = {"seq": seq, "width": 4, "height": 2, "source_width": 4, "source_height": 2,
             "rgb": base64.b64encode(bytes([seq % 255, 30, 50] * 8)).decode(),
             "sampled_at": now, "source_pts_s": time.monotonic(),
             "timestamp_kind": "test-fixture-clock", "clock_mapping_valid": True}
    if mode == "invalid": value["rgb"] = "bad"
    if mode == "stale" and seq > 1: value["sampled_at"] -= 60
    print(json.dumps(value), flush=True)
    time.sleep(0.05)
"""
    )
    binary.chmod(0o700)
    monkeypatch.setattr(camera, "native_helper", lambda root: binary)
    monkeypatch.setattr(
        camera,
        "cameras",
        lambda root: {
            "devices": [
                {
                    "uid": "fixture-physical",
                    "name": "Protocol test fixture",
                    "built_in": True,
                },
                {
                    "uid": "fixture-other",
                    "name": "Second test fixture",
                    "built_in": False,
                },
            ]
        },
    )
    return binary


@pytest.fixture
def camera_runtime(tmp_path, capture_fixture):
    if sys.platform != "darwin":
        pytest.skip("The execution sandbox requires macOS")
    runtime = Runtime(tmp_path / "host", backend="camera")
    try:
        yield runtime
    finally:
        runtime.close()


def submit(runtime, source, devices=None):
    manifest = {
        "entrypoint": "capture.py",
        "files": ["capture.py"],
        "dependencies": {},
        "parameters": {},
        "devices": devices or {"native-camera": ["observe"]},
        "limits": {"wall_seconds": 10},
    }
    version = runtime.request(
        "publish", {"manifest": manifest, "sources": {"capture.py": source}}
    )
    return runtime.request(
        "submit",
        {
            "program_version": version["id"],
            "request_id": uuid.uuid4().hex,
            "based_on": runtime.request("observe", {})["id"],
            "parameters": {},
        },
    )


def finish(runtime, job):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        page = runtime.request(
            "events", {"job_id": job["id"], "after": 10000, "wait": 0.1}
        )
        if page["job"]["status"] in TERMINAL:
            return page["job"]
    pytest.fail("Job did not stop")


def test_camera_job_pixels_permissions_and_evidence(camera_runtime):
    runtime = camera_runtime
    assert runtime.request("health", {})["simulated"] is False
    job = submit(
        runtime,
        'import base64\nfor i in range(3):\n    o=robo.observe()\n    print(len(base64.b64decode(o["pixels"]["rgb"])))\n',
    )
    final = finish(runtime, job)
    assert final["status"] == "exited", final
    verification = final["physical_verification"]
    assert verification["status"] == "unknown"
    assert verification["capture_verification"]["status"] == "succeeded"
    evidence = runtime.request("events", {"job_id": job["id"]})["events"]
    observations = [e["observation"] for e in evidence if e["type"] == "observation"]
    assert len({o["capture_seq"] for o in observations}) == 6
    assert [e.get("text") for e in evidence].count("24") == 3
    for observation in observations:
        assert observation["program_version"] == job["program_version"]
        assert observation["device_uid"] == "fixture-physical"
        assert observation["calibration"]["status"] == "uncalibrated"
        assert runtime.request("media", {"ref": observation["image"]})[
            "base64"
        ].startswith("iVBOR")
    with pytest.raises(CodingError, match="supported device operations"):
        submit(runtime, "pass", {"sim-stage": ["move"]})
    bad = finish(
        runtime, submit(runtime, 'robo.move(1,0,based_on="x",command_id="denied")')
    )
    assert bad["status"] == "failed"
    assert not runtime.store.all("action")


def test_cancel_fences_camera_access_and_protection_releases_capture(camera_runtime):
    runtime = camera_runtime
    job = submit(runtime, "while True:\n    robo.observe()\n")
    runtime.request("cancel", {"job_id": job["id"]})
    final = finish(runtime, job)
    assert final["status"] == "cancelled"
    assert final["stop_confirmation"]["status"] == "confirmed"
    with pytest.raises(CodingError, match="authority"):
        runtime.gateway(job["id"], "observe", {})
    runtime.request("observe", {})  # Shared host observation remains authorized.
    protected = runtime.request("protect", {})
    assert protected["capture_process_running"] is False
    with pytest.raises(CodingError, match="stopped"):
        runtime.request("observe", {})
    root = runtime.store.root
    runtime.hardware.close()
    # Restarting cannot silently reopen a protected device.
    restarted = Runtime(root, backend="camera")
    try:
        assert restarted.request("devices", {})["protection_latched"]
        assert restarted.hardware.stream is None
    finally:
        restarted.close()


def test_runtime_home_pins_backend_and_camera_uid(tmp_path, capture_fixture):
    root = tmp_path / "identity"
    Runtime(root).close()
    with pytest.raises(CodingError, match="another backend"):
        Runtime(root, backend="camera")
    root = tmp_path / "camera-identity"
    Runtime(root, backend="camera").close()
    with pytest.raises(CodingError, match="another device"):
        Runtime(root, backend="camera", camera="fixture-other")
    with pytest.raises(CodingError, match="exactly one camera"):
        Runtime(tmp_path / "missing", backend="camera", camera="missing")


@pytest.mark.parametrize("mode", ["invalid", "denied"])
def test_capture_rejects_invalid_frames_and_permission_failure(capture_fixture, mode):
    with pytest.raises(CodingError) as exc:
        CameraStream(capture_fixture, mode)
    assert exc.value.code == "camera_unavailable"


def test_stale_frames_and_camera_disconnect_never_reuse_old_pixels(capture_fixture):
    stream = CameraStream(capture_fixture, "stale")
    try:
        with pytest.raises(CodingError) as exc:
            stream.latest(after=1, timeout=0.2)
        assert exc.value.code == "camera_timeout"
        stream.process.kill()
        stream.process.wait(timeout=2)
        with pytest.raises(CodingError) as exc:
            stream.latest(after=0)
        assert exc.value.code == "camera_unavailable"
    finally:
        stream.close()
        stream.close()  # Shutdown/protection may both close the same capture.


def test_capture_parent_pipe_eof_releases_device(capture_fixture):
    stream = CameraStream(capture_fixture, "fixture-physical")
    try:
        assert os.getpgid(stream.process.pid) == stream.process.pid
        stream.process.stdin.close()
        assert stream.process.wait(timeout=2) == 0
    finally:
        stream.close()


def test_uncertain_capture_time_cannot_authorize_submission_or_verify_freshness(
    camera_runtime,
):
    runtime = camera_runtime
    observations = [runtime.request("observe", {}) for _ in range(3)]
    for observation in observations:
        observation["clock_mapping_valid"] = False
        observation["source_pts_s"] -= 60
    # Receive-now is useful for viewing, but says nothing about capture freshness.
    with pytest.raises(CodingError, match="stale"):
        runtime._fresh(observations[0], 2)
    result = runtime.hardware.evaluate(observations, {})
    assert result["capture_verification"]["status"] == "unknown"
    assert result["status"] == "unknown"


def test_capture_loss_marks_job_failed_and_result_unknown(camera_runtime):
    runtime = camera_runtime
    job = submit(runtime, "robo.sleep(0.2)\nrobo.observe()\n")
    runtime.hardware.stream.process.kill()
    runtime.hardware.stream.process.wait(timeout=2)
    final = finish(runtime, job)
    assert final["status"] == "failed"
    assert final["physical_verification"]["status"] == "unknown"
    assert "camera" in json.dumps(runtime.request("events", {"job_id": job["id"]}))
