from __future__ import annotations

import copy
import time
import uuid
from contextlib import closing

import pytest

from agent_harness.coding.types import CodingError
from agent_harness.robo.demo import MANIFEST
from agent_harness.robo.hardware import SimHardware
from agent_harness.robo.runtime import TERMINAL, Runtime


def run_program(runtime, source, *, dependencies=None, devices=None):
    manifest = copy.deepcopy(MANIFEST)
    manifest["dependencies"] = dependencies or {}
    if devices:
        manifest["devices"].update(devices)
    version = runtime.request(
        "publish", {"manifest": manifest, "sources": {"control.py": source}}
    )
    job = runtime.request(
        "submit",
        {
            "program_version": version["id"],
            "request_id": uuid.uuid4().hex,
            "parameters": {"gain": 0.65},
            "based_on": runtime.request("observe", {})["id"],
        },
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = runtime.request("status", {"job_id": job["id"]})
        if job["status"] in TERMINAL:
            return job, runtime.store.events(job["id"], 0)["events"]
        time.sleep(0.02)
    pytest.fail("Program did not terminate")


def test_operator_installed_driver_and_ceilings(tmp_path):
    class Device(SimHardware):
        backend = "test-device"

    with closing(
        Runtime(tmp_path, hardware_factory=Device, limits={"wall_seconds": 60})
    ) as runtime:
        assert runtime.request("health", {})["backend"] == "test-device"
        assert runtime.request("devices", {})["program_limits"]["wall_seconds"] == 60
        job, events = run_program(runtime, 'print("driver ready")')
        assert job["exit_code"] == 0, events
    with pytest.raises(CodingError, match="another backend"):
        Runtime(tmp_path)


def test_execution_budget_is_durable_and_preserves_idempotency(tmp_path):
    with closing(Runtime(tmp_path, max_jobs=1)) as runtime:
        assert runtime.request("devices", {})["remaining_jobs"] == 1
        job, _ = run_program(runtime, "pass")
        assert runtime.request("devices", {})["remaining_jobs"] == 0
        args = {
            key: job[key]
            for key in ("program_version", "request_id", "parameters", "based_on")
        }
        assert runtime.request("submit", args)["id"] == job["id"]
        with pytest.raises(CodingError, match="budget exhausted"):
            runtime.request("submit", {**args, "request_id": "new-job"})
    with closing(Runtime(tmp_path, max_jobs=1)) as runtime:
        assert runtime.request("devices", {})["remaining_jobs"] == 0
        assert runtime.request("submit", args)["id"] == job["id"]
        with pytest.raises(CodingError, match="budget exhausted"):
            runtime.request("submit", {**args, "request_id": "another-job"})


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_execution_budget_requires_positive_integer(tmp_path, limit):
    with pytest.raises(CodingError, match="positive integer"):
        Runtime(tmp_path, max_jobs=limit)


def test_admission_window_does_not_relax_local_action_freshness(tmp_path):
    with closing(
        Runtime(tmp_path, limits={"submission_observation_age_s": 60})
    ) as runtime:
        manifest = copy.deepcopy(MANIFEST)
        version = runtime.request(
            "publish", {"manifest": manifest, "sources": {"control.py": "pass"}}
        )
        observation = runtime.request("observe", {})
        old = {**observation, "id": "old-admission", "sampled_at": time.time() - 5}
        runtime.store.put("observation", old["id"], old, immutable=True)
        args = dict(
            program_version=version["id"],
            request_id="admit",
            parameters={"gain": 0.65},
            based_on=old["id"],
        )
        job = runtime.request("submit", args)
        assert job["limits"]["observation_age_s"] == 2
        assert job["based_on"] == old["id"]
        with pytest.raises(CodingError, match="stale"):
            runtime._fresh(old, job["limits"]["observation_age_s"])
        runtime.request("cancel", {"job_id": job["id"]})


def test_numpy_is_frozen_and_only_host_approved_packages_are_allowed(tmp_path):
    pytest.importorskip("numpy")
    with closing(Runtime(tmp_path, packages=("numpy",))) as runtime:
        assert "numpy" in runtime.request("devices", {})["program_dependencies"]
        job, events = run_program(
            runtime,
            "import numpy as np\nprint(int(np.dot([1,2], [3,4])))",
            dependencies={"numpy": "*"},
        )
        assert job["exit_code"] == 0, events
        assert any(e.get("text") == "11" for e in events)
        program = runtime.store.get("program", job["program_version"])
        assert program["runtime"]["identity"]["dependencies"]["numpy"] != "*"
        assert any(
            name.endswith(".so")
            for name in program["runtime"]["identity"]["package_files"]
        )
        manifest = copy.deepcopy(MANIFEST)
        manifest["dependencies"] = {"httpx": "*"}
        with pytest.raises(CodingError, match="host-approved"):
            runtime.request(
                "publish", {"manifest": manifest, "sources": {"control.py": "pass"}}
            )


class ExampleDriver(SimHardware):
    backend = "example-driver"
    grants = {**SimHardware.grants, "api": ["double", "invalid", "lost", "wait"]}

    def invoke(self, action, *, deadline, cancelled):
        from agent_harness.robo.wire import decode, encode

        operation = action["device_operation"]
        if operation == "invalid":
            raise CodingError("device_error", "Invalid API argument")
        if operation == "lost":
            raise RuntimeError("Simulated missing device response")
        if operation == "wait":
            while time.time() < deadline and not cancelled.wait(0.02):
                pass
            return None
        values = decode(action["args"]["args"])
        return encode(values[0] * 2)

    def reconcile(self, action):
        return {"status": "unknown"}


def test_generic_driver_numpy_roundtrip_and_confirmed_api_error(tmp_path):
    pytest.importorskip("numpy")
    with closing(
        Runtime(tmp_path, hardware_factory=ExampleDriver, packages=("numpy",))
    ) as runtime:
        source = """import numpy as np
x = np.arange(12, dtype=np.float32).reshape(3, 4)
assert np.array_equal(robo.call('api', 'double', x), x * 2)
try:
    robo.call('api', 'invalid')
except Exception as exc:
    assert 'device_error' in str(exc)
assert robo.call('api', 'double', 7) == 14
print('driver roundtrip ok')
"""
        job, events = run_program(
            runtime,
            source,
            dependencies={"numpy": "*"},
            devices={"api": ["double", "invalid"]},
        )
        assert job["exit_code"] == 0, events
        actions = runtime.store.all("action")
        assert len(actions) == 3
        assert all(action["status"] == "confirmed" for action in actions)
        assert "error" in actions[1]
        assert all(
            "result" not in event["action"] and "args" not in event["action"]
            for event in events
            if event["type"] in {"action_intent", "action_result"}
        )


def test_call_serializes_arrays_before_capturing_fresh_observation(tmp_path):
    with closing(Runtime(tmp_path, hardware_factory=ExampleDriver)) as runtime:
        source = """import time
import _robo_wire
original = _robo_wire.encode
def slow_encode(value):
    time.sleep(1.1)
    return original(value)
_robo_wire.encode = slow_encode
assert robo.call('api', 'double', 4) == 8
"""
        job, events = run_program(runtime, source, devices={"api": ["double"]})
        assert job["exit_code"] == 0, events


def test_large_call_does_not_reread_durable_job_per_pipe_chunk(tmp_path, monkeypatch):
    with closing(
        Runtime(tmp_path, hardware_factory=ExampleDriver, packages=("numpy",))
    ) as runtime:
        original = runtime._job

        def slow_metadata(key):
            time.sleep(0.015)  # Network-backed SQLite metadata latency.
            return original(key)

        monkeypatch.setattr(runtime, "_job", slow_metadata)
        source = """import numpy as np
x = np.random.default_rng(7).integers(0, 255, (1024, 1024, 3), dtype=np.uint8)
assert np.array_equal(robo.call('api', 'double', x), x * 2)
"""
        job, events = run_program(
            runtime, source, dependencies={"numpy": "*"}, devices={"api": ["double"]}
        )
        assert job["exit_code"] == 0, events


def test_generic_driver_unknown_outcome_fences_new_calls(tmp_path):
    with closing(Runtime(tmp_path, hardware_factory=ExampleDriver)) as runtime:
        source = """try:
    robo.call('api', 'lost')
except Exception as exc:
    assert 'action_unknown' in str(exc)
try:
    robo.call('api', 'double', 7)
    raise RuntimeError('fence bypass')
except Exception as exc:
    assert 'reconciliation_required' in str(exc)
"""
        job, events = run_program(runtime, source, devices={"api": ["lost", "double"]})
        assert job["exit_code"] == 0, events
        assert len(runtime.store.all("action")) == 1
        assert runtime.store.all("action")[0]["status"] == "unknown"


def test_generic_driver_inflight_call_does_not_lock_out_cancel(tmp_path):
    with closing(Runtime(tmp_path, hardware_factory=ExampleDriver)) as runtime:
        manifest = copy.deepcopy(MANIFEST)
        manifest["devices"]["api"] = ["wait"]
        version = runtime.request(
            "publish",
            {
                "manifest": manifest,
                "sources": {"control.py": "robo.call('api', 'wait')"},
            },
        )
        job = runtime.request(
            "submit",
            {
                "program_version": version["id"],
                "request_id": "cancel-test",
                "parameters": {"gain": 0.65},
                "based_on": runtime.request("observe", {})["id"],
            },
        )
        deadline = time.monotonic() + 5
        while not runtime.store.all("action") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert runtime.store.all("action"), "Driver invocation never started"
        started = time.monotonic()
        runtime.request("cancel", {"job_id": job["id"]})
        while time.monotonic() - started < 2:
            status = runtime.request("status", {"job_id": job["id"]})
            if status["status"] == "cancelled":
                break
            time.sleep(0.02)
        assert status["status"] == "cancelled"


def test_wire_rejects_object_arrays_and_decompression_bombs():
    np = pytest.importorskip("numpy")
    from agent_harness.robo.wire import decode, encode

    with pytest.raises(ValueError, match="Unsupported"):
        encode(np.array([object()]))
    packed = encode(np.zeros(10_000, dtype=np.uint8))
    packed["shape"] = [1]
    with pytest.raises(ValueError, match="size"):
        decode(packed)
