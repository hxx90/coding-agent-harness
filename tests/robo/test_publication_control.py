from __future__ import annotations

import copy
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from agent_harness.coding.types import CodingError
from agent_harness.robo.demo import MANIFEST
from agent_harness.robo.runtime import TERMINAL, Runtime


@pytest.mark.parametrize("operation", ["health", "protect"])
def test_publication_does_not_block_control(tmp_path, monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()
    with closing(Runtime(tmp_path)) as runtime:

        def publish(bundle):
            entered.set()
            assert release.wait(5)
            return {"id": "frozen"}

        monkeypatch.setattr(runtime.programs, "publish", publish)
        with ThreadPoolExecutor(max_workers=2) as pool:
            publishing = pool.submit(runtime.request, "publish", {})
            try:
                assert entered.wait(2)
                control = pool.submit(runtime.request, operation, {})
                result = control.result(timeout=1)
                assert result
                assert not publishing.done()
            finally:
                release.set()
            assert publishing.result(timeout=2) == {"id": "frozen"}


def test_publications_serialize_without_blocking_control(tmp_path, monkeypatch):
    entered, queued, release = (threading.Event() for _ in range(3))
    calls = []
    with closing(Runtime(tmp_path)) as runtime:

        def publish(bundle):
            calls.append(bundle["name"])
            entered.set()
            assert release.wait(5)
            return bundle

        def second():
            queued.set()
            return runtime.request("publish", {"name": "second"})

        monkeypatch.setattr(runtime.programs, "publish", publish)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(runtime.request, "publish", {"name": "first"})
            try:
                assert entered.wait(2)
                other = pool.submit(second)
                assert queued.wait(2)
                assert pool.submit(runtime.request, "health", {}).result(timeout=1)
                assert calls == ["first"]
            finally:
                release.set()
            assert first.result(timeout=2) == {"name": "first"}
            assert other.result(timeout=2) == {"name": "second"}


def test_close_stops_hardware_before_waiting_for_publication(tmp_path, monkeypatch):
    entered, release, hardware_closed = (threading.Event() for _ in range(3))
    runtime = Runtime(tmp_path)
    original_close = runtime.hardware.close

    def publish(bundle):
        entered.set()
        assert release.wait(5)
        runtime.store.put("test", "finished-publication", {"finished": True})
        return {"id": "frozen"}

    def close_hardware():
        original_close()
        hardware_closed.set()

    monkeypatch.setattr(runtime.programs, "publish", publish)
    monkeypatch.setattr(runtime.hardware, "close", close_hardware)
    with ThreadPoolExecutor(max_workers=3) as pool:
        publishing = pool.submit(runtime.request, "publish", {})
        try:
            assert entered.wait(2)
            stopped = pool.submit(runtime.close)
            assert hardware_closed.wait(1)
            assert runtime.closing
            assert not stopped.done()
            pending = pool.submit(runtime.request, "publish", {})
        finally:
            release.set()
        assert publishing.result(timeout=2) == {"id": "frozen"}
        stopped.result(timeout=2)
        with pytest.raises(CodingError, match="shutting down"):
            pending.result(timeout=2)
    with pytest.raises(sqlite3.ProgrammingError):
        runtime.store.db.execute("SELECT 1")


def test_publication_error_releases_the_lock(tmp_path, monkeypatch):
    with closing(Runtime(tmp_path)) as runtime:

        def publish(bundle):
            if bundle.get("fail"):
                raise OSError("injected publication error")
            return {"id": "frozen"}

        monkeypatch.setattr(runtime.programs, "publish", publish)
        with pytest.raises(OSError, match="injected publication error"):
            runtime.request("publish", {"fail": True})
        assert runtime.request("publish", {}) == {"id": "frozen"}


def test_running_program_can_stop_while_another_publication_is_waiting(
    tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    with closing(Runtime(tmp_path)) as runtime:
        version = runtime.request(
            "publish",
            {
                "manifest": copy.deepcopy(MANIFEST),
                "sources": {"control.py": "robo.sleep(10)\n"},
            },
        )
        job = runtime.request(
            "submit",
            {
                "program_version": version["id"],
                "request_id": "running",
                "parameters": {"gain": 0.65},
                "based_on": runtime.request("observe", {})["id"],
            },
        )
        started_by = time.monotonic() + 3
        while job["status"] == "starting" and time.monotonic() < started_by:
            time.sleep(0.01)
            job = runtime.request("status", {"job_id": job["id"]})
        assert job["status"] == "running"

        def publish(bundle):
            entered.set()
            assert release.wait(5)
            return {"id": "frozen"}

        def cancel_and_confirm():
            runtime.request("cancel", {"job_id": job["id"]})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                value = runtime.request("status", {"job_id": job["id"]})
                if value["status"] in TERMINAL:
                    return value
                time.sleep(0.01)
            pytest.fail("Program did not stop during blocked publication")

        monkeypatch.setattr(runtime.programs, "publish", publish)
        with ThreadPoolExecutor(max_workers=2) as pool:
            publishing = pool.submit(runtime.request, "publish", {})
            try:
                assert entered.wait(2)
                final = pool.submit(cancel_and_confirm).result(timeout=3)
                assert final["status"] == "cancelled"
                assert final["stop_confirmation"]["status"] == "confirmed"
                assert not publishing.done()
            finally:
                release.set()
            publishing.result(timeout=2)
