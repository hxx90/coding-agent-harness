from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from agent_harness.coding.types import CodingError
from agent_harness.robo.runtime import DEFAULT_LIMITS, Runtime
from agent_harness.robo.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    try:
        yield value
    finally:
        value.db.close()


def reader(store):
    return sqlite3.connect(
        (store.root / "robo.sqlite3").as_uri() + "?mode=ro", uri=True
    )


def test_related_objects_and_events_become_visible_in_one_full_commit(store):
    statements = []
    store.db.set_trace_callback(statements.append)
    job = {"id": "j", "program_version": "v", "counts": {"events": 1}}
    with closing(reader(store)) as witness:
        with store.transaction():
            store.put("job", "j", job)
            event = store.event(job, "log", text="durable")
            assert witness.execute("SELECT count(*) FROM objects").fetchone()[0] == 0
            assert witness.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert witness.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
        assert witness.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert event["seq"] == 1
    assert statements.count("COMMIT") == 1
    assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert not store.db.in_transaction


def test_nested_failure_rolls_back_its_writes_even_when_caught(store):
    with store.transaction():
        store.put("value", "before", {"n": 1})
        with pytest.raises(ValueError):
            with store.transaction():
                store.put("value", "partial", {"n": 2})
                store.event(None, "partial")
                raise ValueError("injected failure")
        store.put("value", "after", {"n": 3})
    assert [value["n"] for value in store.all("value")] == [1, 3]
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert not store.db.in_transaction


def test_outer_failure_rolls_back_immutable_objects_and_nested_success(store):
    with pytest.raises(CodingError, match="Cannot replace"):
        with store.transaction():
            store.put("value", "one", {"n": 1}, immutable=True)
            with store.transaction():
                store.event(None, "pending")
            store.put("value", "one", {"n": 2}, immutable=True)
    assert store.all("value") == []
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert not store.db.in_transaction
    store.put("value", "after", {"n": 3})  # The connection remains usable.


def test_commit_failure_rolls_back_and_leaves_connection_usable(store):
    def authorize(operation, first, second, database, source):
        if operation == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store.db.set_authorizer(authorize)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            with store.transaction():
                store.put("value", "uncommitted", {"n": 1})
                store.event(None, "uncommitted")
    finally:
        store.db.set_authorizer(None)
    assert not store.db.in_transaction
    assert store.all("value") == []
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    store.put("value", "after", {"n": 2})


def gateway(store, invoke):
    """Exercise only the synchronous gateway, without a worker or real hardware."""
    runtime = Runtime.__new__(Runtime)
    runtime.store = store
    runtime.condition = threading.Condition(threading.RLock())
    runtime.hardware = SimpleNamespace(
        invoke=invoke,
        calibration_id="cal",
        operation_devices={"observe": "camera"},
    )
    runtime.workers = {"j": (None, threading.Event())}
    job = {
        "id": "j",
        "program_version": "v",
        "status": "running",
        "deadline": time.time() + 60,
        "counts": {"actions": 0, "events": 0, "log_bytes": 0},
        "limits": dict(DEFAULT_LIMITS),
        "devices": {"api": ["inspect"]},
    }
    store.put("job", "j", job)
    store.put(
        "observation",
        "o",
        {
            "id": "o",
            "job_id": "j",
            "sampled_at": time.time(),
            "quality": "valid",
            "source": "camera",
            "calibration": {"id": "cal"},
        },
    )
    args = {
        "device": "api",
        "operation": "inspect",
        "command_id": "one",
        "based_on": "o",
        "args": [],
        "kwargs": {},
    }
    return runtime, args


def test_device_dispatch_is_after_durable_intent_and_outside_transaction(store):
    def invoke(action, **kwargs):
        assert not store.db.in_transaction
        with closing(reader(store)) as witness:
            objects = {
                kind: json.loads(body)
                for kind, body in witness.execute("SELECT kind,body FROM objects")
            }
            assert objects["job"]["counts"]["actions"] == 1
            assert objects["action"]["status"] == "dispatching"
            assert witness.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        return {"value": 42}

    runtime, args = gateway(store, invoke)
    statements = []
    store.db.set_trace_callback(statements.append)
    assert runtime.gateway("j", "call", args) == {"value": {"value": 42}}
    assert statements.count("COMMIT") == 2  # Intent, then confirmed result.
    assert store.get("action", "j:one")["status"] == "confirmed"
    assert [e["type"] for e in store.events("j", 0)["events"]] == [
        "action_intent",
        "action_result",
    ]


@pytest.mark.parametrize("phase", ["action_intent", "action_result"])
def test_failed_evidence_write_never_acknowledges_or_replays_motion(
    store, monkeypatch, phase
):
    invoked = []
    runtime, args = gateway(
        store, lambda action, **kwargs: invoked.append(action["id"])
    )
    original = store.event

    def failed_event(job, kind, **data):
        if kind == phase:
            raise sqlite3.OperationalError("injected disk failure")
        return original(job, kind, **data)

    monkeypatch.setattr(store, "event", failed_event)
    with pytest.raises(sqlite3.OperationalError, match="disk failure"):
        runtime.gateway("j", "call", args)
    assert not store.db.in_transaction
    if phase == "action_intent":
        assert invoked == []
        assert store.all("action") == []
        assert store.get("job", "j")["counts"]["actions"] == 0
    else:
        assert invoked == ["j:one"]
        assert store.get("action", "j:one")["status"] == "dispatching"
        with pytest.raises(CodingError, match="not confirmed"):
            runtime.gateway("j", "call", args)
        assert invoked == ["j:one"]


@pytest.mark.parametrize("confirmed", [False, True])
def test_driver_error_and_unknown_fence_are_committed_with_result_event(
    store, confirmed
):
    def invoke(action, **kwargs):
        if confirmed:
            raise CodingError("device_error", "confirmed API rejection")
        raise RuntimeError("lost response")

    runtime, args = gateway(store, invoke)
    with pytest.raises(CodingError) as failure:
        runtime.gateway("j", "call", args)
    assert failure.value.code == ("device_error" if confirmed else "action_unknown")
    assert store.get("action", "j:one")["status"] == (
        "confirmed" if confirmed else "unknown"
    )
    assert (store.get("job", "j").get("uncertain_action") == "j:one") is not confirmed
    assert store.events("j", 0)["events"][-1]["type"] == "action_result"
    assert not store.db.in_transaction


def test_log_budget_and_event_are_atomic_without_weakening_limits(store):
    runtime, _ = gateway(store, lambda *args, **kwargs: None)
    statements = []
    store.db.set_trace_callback(statements.append)
    runtime.gateway("j", "log", {"text": "日志"})
    assert statements.count("COMMIT") == 1
    job = store.get("job", "j")
    assert job["counts"] == {"actions": 0, "events": 1, "log_bytes": 6}
    job["limits"]["max_log_bytes"] = 6
    store.put("job", "j", job)
    with pytest.raises(CodingError, match="Log budget exhausted"):
        runtime.gateway("j", "log", {"text": "x"})
    assert store.get("job", "j")["counts"]["log_bytes"] == 7
    assert len(store.events("j", 0)["events"]) == 1
