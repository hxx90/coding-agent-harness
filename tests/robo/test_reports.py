"""Program reports are bounded evidence, never host verification or authority."""

from __future__ import annotations

import copy
import sqlite3
import time
from contextlib import closing

import pytest

from agent_harness.coding.types import CodingError
from agent_harness.robo.runtime import DEFAULT_LIMITS, Runtime
from agent_harness.robo.store import canonical
from agent_harness.robo.tools import event_page

from .test_event_wait_integration import drain, run_program


@pytest.fixture
def reporting(tmp_path):
    with closing(Runtime(tmp_path)) as runtime:
        job = {
            "id": "j",
            "program_version": "v",
            "status": "running",
            "deadline": time.time() + 60,
            "counts": {"actions": 0, "events": 0, "log_bytes": 0},
            "limits": dict(DEFAULT_LIMITS),
            "devices": {},
            "physical_verification": {"status": "unknown"},
            "stop_confirmation": {"status": "unknown"},
        }
        runtime.store.put("job", "j", job)
        for name, owner, version in (("o", "j", "v"), ("foreign", "other", "w")):
            runtime.store.put(
                "observation",
                name,
                {
                    "id": name,
                    "job_id": owner,
                    "program_version": version,
                    "sampled_at": 123.0,
                    "quality": "valid",
                    "source": runtime.hardware.operation_devices["observe"],
                    "calibration": {"id": runtime.hardware.calibration_id},
                },
            )
        yield runtime


REPORT = {
    "stage": "effect check",
    "status": "fail",
    "observations": ["o"],
    "measurements": {"before": 1.25, "after": 1.25, "visible": True},
}


def test_report_is_atomic_durable_bounded_and_separate_from_verification(reporting):
    runtime = reporting
    original = copy.deepcopy(REPORT)
    statements = []
    runtime.store.db.set_trace_callback(statements.append)
    result = runtime.gateway("j", "report", REPORT)
    assert REPORT == original
    assert result["type"] == "program_report"
    assert result["source"] == "program"
    assert result["job_id"] == "j" and result["program_version"] == "v"
    assert result["observations"] == [{"id": "o", "sampled_at": 123.0}]
    assert result["measurements"] == REPORT["measurements"]
    job = runtime.store.get("job", "j")
    assert job["latest_report"] == result
    assert job["status"] == "running"  # Program branches, not the reporting API.
    assert job["physical_verification"] == {"status": "unknown"}
    assert job["stop_confirmation"] == {"status": "unknown"}
    assert job["counts"]["actions"] == 0
    assert job["counts"]["events"] == 1
    assert 0 < job["counts"]["log_bytes"] <= 2048
    assert statements.count("COMMIT") == 1
    assert runtime.store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    persisted = runtime.store.events("j", 0)["events"][0]
    assert persisted == result
    snapshot = runtime.request("snapshot", {"job_ids": ["j"]})
    assert snapshot["jobs"][0]["latest_report"] == result
    assert (
        event_page({"job": job, "events": [result]})["job"]["latest_report"] == result
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"stage": ""},
        {"stage": " "},
        {"stage": "s" * 129},
        {"stage": None},
        {"status": "success"},
        {"status": True},
        {"status": []},
        {"measurements": []},
        {"measurements": {"value": float("nan")}},
        {"measurements": {"value": float("inf")}},
        {"measurements": {"value": b"not JSON"}},
        {"measurements": {"value": "a" * 2048}},
        {"measurements": {"value": "字" * 700}},
        {"observations": "o"},
        {"observations": ["o"] * 5},
        {"observations": [None]},
        {"observations": ["missing"]},
        {"observations": ["foreign"]},
        {"physical_verification": {"status": "success"}},
    ],
)
def test_invalid_report_never_changes_evidence_or_authority(reporting, changes):
    before = reporting.store.get("job", "j")
    with pytest.raises(CodingError):
        reporting.gateway("j", "report", {**REPORT, **changes})
    assert reporting.store.get("job", "j") == before
    assert reporting.store.events("j", 0)["events"] == []
    assert reporting.store.all("action") == []


@pytest.mark.parametrize(
    "measurements, message",
    [
        ({"value": float("nan")}, "finite JSON"),
        ({"nested": [float("inf")]}, "finite JSON"),
        ({"value": b"not JSON"}, "finite JSON"),
        ({"value": "a" * 2048}, "exceeds 2048 UTF-8 bytes"),
        ({"value": "字" * 700}, "exceeds 2048 UTF-8 bytes"),
    ],
)
def test_report_error_distinguishes_invalid_values_from_size(
    reporting, measurements, message
):
    before = reporting.store.get("job", "j")
    with pytest.raises(CodingError, match=message) as error:
        reporting.gateway("j", "report", {**REPORT, "measurements": measurements})
    assert error.value.code == "invalid_report"
    assert reporting.store.get("job", "j") == before
    assert reporting.store.events("j", 0)["events"] == []
    assert reporting.store.all("action") == []


@pytest.mark.parametrize("status", ["pass", "fail", "unknown"])
def test_all_report_statuses_remain_program_claims(reporting, status):
    reporting.gateway("j", "report", {**REPORT, "status": status, "observations": []})
    job = reporting.store.get("job", "j")
    assert job["latest_report"]["status"] == status
    assert job["physical_verification"]["status"] == "unknown"
    assert job["status"] == "running"


def test_report_does_not_refresh_observation_or_uncertain_action(reporting):
    job = reporting.store.get("job", "j")
    job["uncertain_action"] = "lost-receipt"
    job["devices"] = {"sim-stage": ["move"]}
    reporting.store.put("job", "j", job)
    reporting.gateway("j", "report", {**REPORT, "status": "pass"})
    after = reporting.store.get("job", "j")
    assert after["uncertain_action"] == "lost-receipt"
    assert reporting.store.get("observation", "o")["sampled_at"] == 123.0
    with pytest.raises(CodingError, match="stale"):
        reporting._fresh(reporting.store.get("observation", "o"), 2)
    with pytest.raises(CodingError, match="Query uncertain command"):
        reporting.gateway("j", "move", {"command_id": "dependent"})
    assert reporting.store.all("action") == []


def test_report_shares_log_budget_without_silent_truncation(reporting):
    job = reporting.store.get("job", "j")
    job["limits"]["max_log_bytes"] = 300
    reporting.store.put("job", "j", job)
    first = reporting.gateway("j", "report", REPORT)
    charged = reporting.store.get("job", "j")["counts"]["log_bytes"]
    assert 0 < charged < 300
    with pytest.raises(CodingError, match="Log budget exhausted"):
        reporting.gateway("j", "log", {"text": "x" * (301 - charged)})
    assert reporting.store.events("j", 0)["events"] == [first]
    assert reporting.store.get("job", "j")["latest_report"] == first


def test_report_uses_event_budget(reporting):
    job = reporting.store.get("job", "j")
    job["limits"]["max_events"] = 1
    reporting.store.put("job", "j", job)
    reporting.gateway("j", "report", REPORT)
    with pytest.raises(CodingError, match="Event budget exhausted"):
        reporting.gateway("j", "report", REPORT)
    assert len(reporting.store.events("j", 0)["events"]) == 1


def test_report_cannot_bypass_existing_log_budget(reporting):
    first = reporting.gateway("j", "report", REPORT)
    job = reporting.store.get("job", "j")
    job["counts"]["log_bytes"] = job["limits"]["max_log_bytes"] - 1
    reporting.store.put("job", "j", job)
    with pytest.raises(CodingError, match="Log budget exhausted"):
        reporting.gateway("j", "report", {**REPORT, "status": "pass"})
    assert reporting.store.events("j", 0)["events"] == [first]
    assert reporting.store.get("job", "j")["latest_report"] == first


def test_expired_job_cannot_report(reporting):
    job = reporting.store.get("job", "j")
    job["deadline"] = time.time() - 1
    reporting.store.put("job", "j", job)
    with pytest.raises(CodingError, match="authority"):
        reporting.gateway("j", "report", REPORT)
    assert reporting.store.events("j", 0)["events"] == []


def test_report_version_must_match_its_observation(reporting):
    observation = reporting.store.get("observation", "o")
    observation["program_version"] = "other-version"
    reporting.store.put("observation", "o", observation)
    with pytest.raises(CodingError, match="another job"):
        reporting.gateway("j", "report", REPORT)
    assert reporting.store.events("j", 0)["events"] == []


def test_failed_commit_does_not_publish_a_phantom_report(reporting):
    before = reporting.store.get("job", "j")

    def authorize(operation, first, second, database, source):
        if operation == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    reporting.store.db.set_authorizer(authorize)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            reporting.gateway("j", "report", REPORT)
    finally:
        reporting.store.db.set_authorizer(None)
    assert reporting.store.get("job", "j") == before
    assert reporting.store.events("j", 0)["events"] == []
    assert reporting.gateway("j", "report", REPORT)["status"] == "fail"


@pytest.mark.parametrize("state", ["exited", "cancelled", "failed", "cancelling"])
def test_stopped_jobs_cannot_report(reporting, state):
    job = reporting.store.get("job", "j")
    job["status"] = state
    reporting.store.put("job", "j", job)
    with pytest.raises(CodingError, match="authority"):
        reporting.gateway("j", "report", REPORT)
    assert reporting.store.events("j", 0)["events"] == []


@pytest.mark.parametrize("status,actions", [("pass", 1), ("fail", 0), ("unknown", 0)])
def test_real_worker_report_and_plain_python_guard(host, status, actions):
    client, _, _ = host
    source = f"""def main():
    o = robo.observe()
    status = {status!r}
    robo.report("precondition", status, observations=[o["id"]],
                measurements={{"visible": status != "unknown"}})
    if status != "pass":
        return
    # Refresh the action basis; a report never grants fresh motion authority.
    o = robo.observe()
    robo.move(0.1, 0, based_on=o["id"], command_id="dependent")
main()
"""
    job = run_program(client, source)
    page = client.request("events", job_id=job["id"], until="terminal", wait=30)
    assert page["job"]["status"] == "exited", page
    assert page["job"]["counts"]["actions"] == actions
    assert page["job"]["stop_confirmation"]["status"] == "confirmed"
    report = next(e for e in drain(client, job["id"]) if e["type"] == "program_report")
    assert report["status"] == status
    snapshot = client.request("snapshot", job_ids=[job["id"]])
    assert snapshot["jobs"][0]["latest_report"] == report
    assert snapshot["jobs"][0]["physical_verification"]["status"] == "failed"


def test_report_stays_visible_beyond_a_full_log_page(host):
    client, process, start = host
    source = """for i in range(105):
    print("history", i)
o = robo.observe()
robo.report("effect", "unknown", observations=[o["id"]],
            measurements={"visible": False})
print("later debug text is not a replacement for the report")
"""
    job = run_program(client, source)
    page = client.request("events", job_id=job["id"], until="terminal", wait=30)
    assert page["job"]["status"] == "exited", page
    assert len(page["events"]) == 100
    assert not any(e["type"] == "program_report" for e in page["events"])
    report = page["job"]["latest_report"]
    assert report["seq"] > page["cursor"]
    assert report["status"] == "unknown"
    all_events = drain(client, job["id"])
    assert report in all_events
    assert len([e for e in all_events if e["type"] == "log"]) == 106
    assert len(canonical(report).encode()) < 2048
    process.terminate()
    process.communicate(timeout=15)
    restarted, _ = start()
    snapshot = restarted.request("snapshot", job_ids=[job["id"]])
    assert snapshot["jobs"][0]["latest_report"] == report
    assert drain(restarted, job["id"]) == all_events  # No replay on restart.
