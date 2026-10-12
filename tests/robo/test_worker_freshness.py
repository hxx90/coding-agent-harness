import io
import json
import sys
import threading
import time
from contextlib import closing

import pytest
from agent_harness.coding.types import CodingError
from agent_harness.robo import wire
from agent_harness.robo.hardware import SimHardware
from agent_harness.robo.runtime import DEFAULT_LIMITS, Runtime
from agent_harness.robo.worker import DeviceError
from agent_harness.robo.worker import Robo as CandidateRobo


@pytest.fixture(autouse=True)
def wire_alias(monkeypatch):
    monkeypatch.setitem(sys.modules, "_robo_wire", wire)


def client():
    return CandidateRobo(
        dict(parameters={}, job_id="test-job", program_version="test-program")
    )


def exchange(monkeypatch, replies):
    output = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", output)
    monkeypatch.setattr(
        sys, "__stdin__", io.StringIO("".join(json.dumps(x) + "\n" for x in replies))
    )
    return output


def test_exact_stale_code_refreshes_once_with_same_command(monkeypatch):
    out = exchange(
        monkeypatch,
        [
            {"result": {"id": "old"}},
            {"error": "stale_observation"},
            {"result": {"id": "new"}},
            {"result": {"value": 17}},
        ],
    )
    assert client().call("capx", "open_gripper") == 17
    requests = [json.loads(x) for x in out.getvalue().splitlines()]
    calls = [x["args"] for x in requests if x["op"] == "call"]
    assert len(calls) == 2
    assert calls[0]["command_id"] == calls[1]["command_id"] == "call-1"
    assert calls[0]["based_on"] == "old" and calls[1]["based_on"] == "new"
    assert {**calls[0], "based_on": "new"} == calls[1]


def test_repeated_stale_is_bounded(monkeypatch):
    out = exchange(
        monkeypatch,
        [
            {"result": {"id": "old"}},
            {"error": "stale_observation"},
            {"result": {"id": "new"}},
            {"error": "stale_observation"},
        ],
    )
    with pytest.raises(DeviceError) as error:
        client().call("capx", "open_gripper")
    assert error.value.code == "stale_observation"
    assert len(out.getvalue().splitlines()) == 4


@pytest.mark.parametrize(
    "code",
    [
        "action_unknown",
        "device_error",
        "reconciliation_required",
        "device_grant",
        "job_stopped",
        "observation_conflict",
    ],
)
def test_other_errors_never_retry_even_with_stale_text(monkeypatch, code):
    out = exchange(
        monkeypatch,
        [{"result": {"id": "old"}}, {"error": code, "message": "stale_observation"}],
    )
    with pytest.raises(DeviceError) as error:
        client().call("capx", "open_gripper")
    assert error.value.code == code
    assert len(out.getvalue().splitlines()) == 2


def test_lost_pipe_never_retries(monkeypatch):
    out = exchange(monkeypatch, [{"result": {"id": "old"}}])
    with pytest.raises(DeviceError, match="disconnected"):
        client().call("capx", "open_gripper")
    assert len(out.getvalue().splitlines()) == 2


def test_real_host_rejects_before_intent_then_dispatches_same_id_once(tmp_path):
    invocations = []

    class Driver(SimHardware):
        grants = {**SimHardware.grants, "api": ["act"]}

        def invoke(self, action, **kwargs):
            invocations.append(action)
            return 17

    with closing(Runtime(tmp_path, hardware_factory=Driver)) as runtime:
        job = dict(
            id="test-job",
            program_version="test-program",
            status="running",
            deadline=time.time() + 60,
            devices=Driver.grants,
            limits=DEFAULT_LIMITS,
            counts=dict(actions=0, events=0, log_bytes=0),
        )
        runtime.store.put("job", job["id"], job)
        runtime.workers[job["id"]] = (threading.Thread(), threading.Event())
        base = runtime.hardware.observe(job)
        old = {**base, "id": "old", "sampled_at": time.time() - 3}
        runtime.store.put("observation", "old", old, immutable=True)
        asks = []

        def request(op, **args):
            asks.append((op, args))
            if op == "observe":
                return old if len(asks) == 1 else runtime.hardware.observe(job)
            try:
                return runtime.gateway(job["id"], op, args)
            except CodingError as exc:
                assert exc.code == "stale_observation"
                assert not invocations and not runtime.store.all("action")
                assert not any(
                    e["type"] == "action_intent"
                    for e in runtime.store.events(job["id"], 0)["events"]
                )
                raise DeviceError(str(exc), code=exc.code) from exc

        sdk = client()
        sdk._request = request
        try:
            assert sdk.call("api", "act") == 17
            assert len(invocations) == 1
            actions = runtime.store.all("action")
            assert len(actions) == 1 and actions[0]["command_id"] == "call-1"
            assert actions[0]["status"] == "confirmed"
            assert runtime.store.get("job", job["id"])["counts"]["actions"] == 1
        finally:
            runtime.workers.clear()
