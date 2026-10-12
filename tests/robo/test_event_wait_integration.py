"""Terminal subscriptions over real sockets must not change execution authority."""

from __future__ import annotations

import asyncio
import copy
import json
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_harness.coding.types import Cancelled
from agent_harness.robo.client import Client, socket_path
from agent_harness.robo.demo import MANIFEST, wait_job
from agent_harness.robo.runtime import TERMINAL


def run_program(client, source, *, policy="continue_bounded", seconds=15):
    manifest = copy.deepcopy(MANIFEST)
    manifest["limits"]["wall_seconds"] = seconds
    version = client.request(
        "publish", manifest=manifest, sources={"control.py": source}
    )
    return client.request(
        "submit",
        program_version=version["id"],
        request_id=uuid.uuid4().hex,
        parameters={"gain": 0.65},
        based_on=client.request("observe")["id"],
        disconnect_policy=policy,
    )


def drain(client, job_id):
    cursor, result = 0, []
    while True:
        page = client.request("events", job_id=job_id, after=cursor)
        if not page["events"]:
            return result
        cursor = page["cursor"]
        result.extend(page["events"])


@pytest.mark.parametrize(
    "source,expected,seconds",
    [
        # Exercise both sockets beyond their old 20 s I/O timeout.
        ("robo.sleep(20.5)\nprint('finished')\n", "exited", 25),
        ("robo.sleep(0.3)\nraise RuntimeError('intentional failure')\n", "failed", 15),
    ],
)
def test_real_worker_terminal_wait_and_stop_confirmation(
    host, source, expected, seconds
):
    client, _, _ = host
    job = run_program(client, source, seconds=seconds)
    began = time.monotonic()
    page = client.request("events", job_id=job["id"], until="terminal", wait=30)
    assert page["job"]["status"] == expected
    assert page["job"]["stop_confirmation"]["status"] == "confirmed"
    assert page["job"]["stop_confirmation"]["moving"] is False
    assert time.monotonic() - began < 30
    if expected == "exited":
        assert time.monotonic() - began > 20
        assert any(e.get("text") == "finished" for e in page["events"])
    else:
        assert "intentional failure" in json.dumps(page)


def test_real_worker_terminal_pages_preserve_every_event(host):
    client, _, _ = host
    job = run_program(
        client, "for i in range(205):\n    print('event', i)\nrobo.sleep(0.2)\n"
    )
    page = client.request("events", job_id=job["id"], until="terminal", wait=30)
    assert page["job"]["status"] == "exited"
    assert len(page["events"]) == 100
    timeline = list(page["events"])
    while page["events"]:
        page = client.request(
            "events", job_id=job["id"], after=page["cursor"], until="terminal", wait=30
        )
        timeline.extend(page["events"])
    assert timeline == drain(client, job["id"])
    assert [e["text"] for e in timeline if e["type"] == "log"] == [
        f"event {i}" for i in range(205)
    ]


MOVING = """i = 0
while True:
    o = robo.observe()
    robo.move(0.1 if i % 2 else -0.1, 0, based_on=o["id"], command_id=str(i))
    i += 1
    robo.sleep(0.3)
"""


@pytest.mark.parametrize("operation", ["cancel", "protect"])
def test_second_client_can_stop_motion_during_terminal_wait(host, operation):
    client, _, _ = host
    job = run_program(client, MOVING)
    for page in client.subscribe(job["id"]):
        if any(e["type"] == "action_result" for e in page["events"]):
            break
    controller = Client(client.root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(
            client.request, "events", job_id=job["id"], until="terminal", wait=30
        )
        time.sleep(0.15)
        assert not pending.done()
        assert (
            pool.submit(controller.request, "health").result(timeout=1)["status"]
            == "ready"
        )
        kwargs = {"job_id": job["id"]} if operation == "cancel" else {}
        pool.submit(controller.request, operation, **kwargs).result(timeout=1)
        assert pending.result(timeout=2)["job"]["status"] in {"cancelling", "cancelled"}
    final = wait_job(controller, job["id"])
    assert final["status"] == "cancelled"
    assert final["stop_confirmation"]["status"] == "confirmed"
    assert final["stop_confirmation"]["moving"] is False
    timeline = drain(controller, job["id"])
    cancelled_at = next(e["seq"] for e in timeline if e["type"] == "cancel_requested")
    assert not any(
        e["type"] == "action_intent" and e["seq"] > cancelled_at for e in timeline
    )


def test_interrupting_wait_does_not_cancel_continue_bounded_job(host):
    client, _, _ = host
    job = run_program(client, "robo.sleep(1)\nprint('survived disconnect')\n")
    stop = threading.Event()
    subscriber = Client(client.root, stop)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            subscriber.request, "events", job_id=job["id"], until="terminal", wait=30
        )
        time.sleep(0.15)
        assert not pending.done()
        stop.set()
        with pytest.raises(Cancelled):
            pending.result(timeout=1)
    final = wait_job(client, job["id"])
    assert final["status"] == "exited"
    timeline = drain(client, job["id"])
    assert not any(e["type"] == "cancel_requested" for e in timeline)
    assert any(e.get("text") == "survived disconnect" for e in timeline)


def test_disconnected_terminal_wait_never_renews_cancel_lease(host):
    client, _, _ = host
    job = run_program(client, MOVING, policy="cancel")
    started = time.monotonic()
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(socket_path(client.root)))
        connection.sendall(
            (
                json.dumps(
                    {
                        "protocol": 1,
                        "op": "events",
                        "args": {"job_id": job["id"], "until": "terminal", "wait": 30},
                    }
                )
                + "\n"
            ).encode()
        )
        # Status reads do not renew leases; ensure the subscription was accepted.
        while time.monotonic() - started < 2:
            value = client.request("status", job_id=job["id"])
            if value["client_deadline"] > job["client_deadline"]:
                break
            time.sleep(0.02)
        else:
            pytest.fail("Host did not accept the terminal subscription")
        renewed_deadline = value["client_deadline"]
    while time.monotonic() - started < 8:
        value = client.request("status", job_id=job["id"])
        assert value["client_deadline"] == renewed_deadline
        if value["status"] in TERMINAL:
            break
        time.sleep(0.05)
    assert value["status"] == "cancelled"
    assert value["stop_confirmation"]["status"] == "confirmed"
    assert value["stop_confirmation"]["moving"] is False
    assert time.monotonic() - started < 8
    assert any(e["type"] == "action_result" for e in drain(client, job["id"]))


@pytest.mark.parametrize(
    "operation,args,expected",
    [
        ("events", {"until": "terminal", "wait": 30}, 40),
        ("events", {"until": "events", "wait": 10}, 20),
        ("events", {}, 20),
        ("status", {"until": "terminal"}, 20),
        ("publish", {}, 120),
    ],
)
def test_transport_timeout_is_scoped_to_terminal_events(
    tmp_path, monkeypatch, operation, args, expected
):
    timeouts = []
    original = asyncio.timeout

    def timeout(seconds):
        timeouts.append(seconds)
        return original(seconds)

    class Writer:
        def write(self, data):
            assert json.loads(data)["op"] == operation

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"result":{"ok":true}}\n')
        reader.feed_eof()
        return reader, Writer()

    monkeypatch.setattr(asyncio, "timeout", timeout)
    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    assert Client(tmp_path).request(operation, **args) == {"ok": True}
    assert timeouts == [expected]
