from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest

from agent_harness.coding.types import CodingError
from agent_harness.robo import runtime as runtime_module
from agent_harness.robo.runtime import Runtime


@pytest.fixture
def waiting_job(tmp_path):
    with closing(Runtime(tmp_path)) as runtime:
        job = {
            "id": "waiting-job",
            "program_version": "frozen-version",
            "status": "running",
            "disconnect_policy": "continue_bounded",
            "deadline": 10000,
            "client_deadline": 100,
            "counts": {"actions": 0},
        }
        runtime.store.put("job", job["id"], job)
        runtime.store.event(job, "log", text="checkpoint")
        yield runtime, job


def watch_wait(runtime, monkeypatch):
    entered = threading.Event()
    original = runtime.condition.wait

    def wait(timeout):
        entered.set()
        return original(timeout)

    monkeypatch.setattr(runtime.condition, "wait", wait)
    return entered


def terminal_args(job, **extra):
    return {"job_id": job["id"], "until": "terminal", "wait": 30, **extra}


def test_terminal_wait_batches_existing_events_and_preserves_pagination(
    waiting_job, monkeypatch
):
    runtime, job = waiting_job
    for index in range(204):
        runtime.store.event(job, "log", text=f"log-{index}")
    entered = watch_wait(runtime, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(runtime.request, "events", terminal_args(job))
        try:
            assert entered.wait(1), "Existing events must not end a terminal wait"
            assert not pending.done()
        finally:
            with runtime.condition:
                job.update(
                    status="exited",
                    exit_code=0,
                    stop_confirmation={"status": "confirmed", "moving": False},
                )
                runtime._save(job)
        page = pending.result(timeout=2)
    assert page["job"] == job
    assert len(page["events"]) == 100
    all_events = list(page["events"])
    while page["events"]:
        page = runtime.request("events", terminal_args(job, after=page["cursor"]))
        all_events.extend(page["events"])
    assert len(all_events) == 205
    assert [e["text"] for e in all_events] == [
        "checkpoint",
        *(f"log-{index}" for index in range(204)),
    ]
    assert len({e["seq"] for e in all_events}) == 205


def test_default_event_stream_still_returns_available_events(waiting_job, monkeypatch):
    runtime, job = waiting_job

    def unexpected_wait(timeout):
        pytest.fail("The default event stream must return its available page")

    monkeypatch.setattr(runtime.condition, "wait", unexpected_wait)
    page = runtime.request("events", {"job_id": job["id"], "wait": 10})
    assert page["job"]["status"] == "running"
    assert [e["text"] for e in page["events"]] == ["checkpoint"]


@pytest.mark.parametrize(
    "policy,expected_wait", [("continue_bounded", 30), ("cancel", 4)]
)
def test_wait_is_bounded_and_never_renews_cancel_lease_on_wakeup(
    waiting_job, monkeypatch, policy, expected_wait
):
    runtime, job = waiting_job
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        runtime_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now),
    )
    waits = []

    def wait(timeout):
        waits.append(timeout)
        clock.now += timeout

    monkeypatch.setattr(runtime.condition, "wait", wait)
    job["disconnect_policy"] = policy
    runtime.store.put("job", job["id"], job)
    page = runtime.request("events", terminal_args(job))
    assert sum(waits) == expected_wait
    assert page["job"]["status"] == "running"
    assert page["job"]["deadline"] == 10000
    assert page["job"]["client_deadline"] == (1005 if policy == "cancel" else 100)
    assert page["events"][0]["text"] == "checkpoint"


@pytest.mark.parametrize(
    "change",
    [
        {"status": "failed", "error": "device error"},
        {"status": "cancelled", "stop_confirmation": {"status": "confirmed"}},
        {"status": "timed_out"},
        {"status": "interrupted"},
        {"status": "cancelling"},
        {"uncertain_action": "original-command"},
    ],
)
def test_terminal_wait_returns_terminal_or_intervention_state_without_hiding_it(
    waiting_job, monkeypatch, change
):
    runtime, job = waiting_job
    job.update(change)
    runtime.store.put("job", job["id"], job)

    def unexpected_wait(timeout):
        pytest.fail("A terminal or intervention state must wake the caller")

    monkeypatch.setattr(runtime.condition, "wait", unexpected_wait)
    page = runtime.request("events", terminal_args(job))
    assert page["job"] == job
    assert page["events"][0]["text"] == "checkpoint"
    assert runtime.store.all("action") == []


def test_terminal_wait_does_not_block_health_or_protection(waiting_job, monkeypatch):
    runtime, job = waiting_job
    entered = watch_wait(runtime, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(runtime.request, "events", terminal_args(job))
        assert entered.wait(1)
        assert pool.submit(runtime.request, "health", {}).result(timeout=1)
        stopped = pool.submit(runtime.request, "protect", {}).result(timeout=1)
        assert stopped["status"] == "confirmed"
        assert pending.result(timeout=2)["job"]["status"] == "cancelling"


def test_close_wakes_waiters_before_they_access_closed_database(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    job = {
        "id": "j",
        "program_version": "v",
        "status": "running",
        "disconnect_policy": "continue_bounded",
    }
    runtime.store.put("job", job["id"], job)
    entered = watch_wait(runtime, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(runtime.request, "events", terminal_args(job))
        try:
            assert entered.wait(1)
        finally:
            runtime.close()
        with pytest.raises(CodingError, match="shutting down"):
            pending.result(timeout=2)
    with pytest.raises(CodingError, match="shutting down"):
        runtime.request("events", terminal_args(job))


@pytest.mark.parametrize(
    "extra",
    [
        {"until": "other"},
        {"until": None},
        {"until": True},
        {"wait": 31},
        {"wait": -1},
        {"wait": True},
        {"wait": float("nan")},
        {"until": "events", "wait": 11},
    ],
)
def test_invalid_wait_does_not_renew_a_lease_or_mutate_events(waiting_job, extra):
    runtime, job = waiting_job
    job["disconnect_policy"] = "cancel"
    runtime.store.put("job", job["id"], job)
    before = copy.deepcopy(runtime.store.events(job["id"], 0))
    with pytest.raises(CodingError):
        runtime.request("events", terminal_args(job, **extra))
    assert runtime.store.get("job", job["id"]) == job
    assert runtime.store.events(job["id"], 0) == before
