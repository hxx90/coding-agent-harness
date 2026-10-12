"""Compact model evidence must retain authority, errors and temporal context."""

from __future__ import annotations

import copy
import json
import base64
import threading
from types import SimpleNamespace

from agent_harness.coding.media import MediaStore
from agent_harness.robo.hardware import png

from agent_harness.robo.tools import event_page


def test_event_page_preserves_safety_and_does_not_change_durable_input():
    job = {
        "id": "job_" + "j" * 32,
        "program_version": "a" * 64,
        "status": "failed",
        "deadline": 20,
        "disconnect_policy": "continue_bounded",
        "uncertain_action": "original-command",
        "stop_confirmation": {"status": "unknown"},
        "physical_verification": {"status": "unknown", "window": [10, 12]},
        "counts": {"actions": 1},
        "error": "lost reply",
        "hardware": {"calibration": "large-static-description" * 200},
        "devices": {"arm": ["move"]},
        "limits": {"max_actions": 250},
        "parameters": {},
        "goal": {"method": "driver-defined"},
        "future_safety_field": "must not be silently removed",
    }
    action = {
        "job_id": job["id"],
        "program_version": job["program_version"],
        "command_id": "original-command",
        "status": "unknown",
        "error": "lost reply",
        "timestamp": 10,
    }
    observation = {
        "id": "obs-1",
        "sampled_at": 11,
        "received_at": 12,
        "quality": "valid",
        "frame": "camera",
        "unit": "pixel",
        "image": {"id": "hash", "metadata": {"sampled_at": 11}},
    }
    page = {
        "job": job,
        "cursor": 3,
        "events": [
            {
                "job_id": job["id"],
                "program_version": job["program_version"],
                "type": "action_result",
                "seq": 1,
                "timestamp": 11,
                "action": action,
            },
            {
                "job_id": job["id"],
                "program_version": job["program_version"],
                "type": "observation",
                "seq": 2,
                "timestamp": 12,
                "observation": observation,
            },
            {
                "job_id": job["id"],
                "program_version": job["program_version"],
                "type": "log",
                "seq": 3,
                "timestamp": 13,
                "text": "verbatim evidence",
            },
        ],
    }
    original = copy.deepcopy(page)
    shown = event_page(page)
    assert page == original
    assert shown["cursor"] == 3
    for key in (
        "id",
        "program_version",
        "status",
        "deadline",
        "disconnect_policy",
        "uncertain_action",
        "stop_confirmation",
        "physical_verification",
        "counts",
        "error",
        "future_safety_field",
    ):
        assert shown["job"][key] == job[key]
    for key in ("command_id", "status", "error", "timestamp"):
        assert shown["events"][0]["action"][key] == action[key]
    assert shown["events"][1]["observation"] == observation
    assert shown["events"][2]["text"] == "verbatim evidence"
    assert [e["seq"] for e in shown["events"]] == [1, 2, 3]
    assert "job_id" not in shown["events"][0]
    assert "program_version" not in shown["events"][0]["action"]
    assert len(json.dumps(shown)) < len(json.dumps(page)) / 2


def test_event_page_keeps_different_identities_and_unknown_fields():
    page = {
        "cursor": 5,
        "job": {"id": "j", "program_version": "v"},
        "events": [
            {
                "job_id": "other-job",
                "program_version": "other-version",
                "type": "future",
                "seq": 5,
                "data": {"job_id": "j"},
            }
        ],
        "future_page_field": "preserve",
    }
    shown = event_page(page)
    assert shown["events"] == page["events"]
    assert shown["future_page_field"] == "preserve"


def test_event_page_with_no_identity_does_not_drop_null_fields():
    page = {"job": {}, "events": [{"job_id": None, "type": "diagnostic"}]}
    assert event_page(page)["events"] == page["events"]


def test_nested_observation_inherits_only_exact_job_and_version_identities():
    observation = {
        "job_id": "j",
        "program_version": "v",
        "id": "obs-1",
        "sampled_at": 1,
        "received_at": 2,
        "quality": "valid",
        "image": {"id": "image-1", "metadata": {"sampled_at": 1}},
        "future_field": {"job_id": "j"},
    }
    different = {**observation, "job_id": "other", "program_version": "different"}
    page = {
        "job": {"id": "j", "program_version": "v"},
        "cursor": 2,
        "events": [
            {"type": "observation", "seq": 1, "observation": observation},
            {"type": "observation", "seq": 2, "observation": different},
        ],
    }
    before = copy.deepcopy(page)
    shown = event_page(page)
    assert page == before
    assert shown["events"][0]["observation"] == {
        k: v for k, v in observation.items() if k not in {"job_id", "program_version"}
    }
    assert shown["events"][1]["observation"] == different
    restored = copy.deepcopy(shown)
    restored["events"][0]["observation"].update(job_id="j", program_version="v")
    assert restored == page


def test_tool_keeps_last_four_observations_even_before_trailing_logs(
    tmp_path, monkeypatch
):
    from agent_harness.robo import tools

    media = MediaStore(tmp_path / "source-media")
    refs = [
        media.put(png(2, 2, bytes([i] * 12)), "image/png", metadata={"sampled_at": i})
        for i in range(6)
    ]
    page = {
        "job": {"id": "j", "program_version": "v"},
        "cursor": 12,
        "events": [
            {"type": "observation", "observation": {"image": ref}} for ref in refs
        ]
        + [{"type": "log", "text": "trailing log"} for _ in range(6)],
    }

    class Client:
        def __init__(self, *args):
            pass

        def request(self, op, **args):
            if op == "events":
                return copy.deepcopy(page)
            assert op == "media"
            return {"base64": base64.b64encode(media.get(args["ref"])).decode()}

    monkeypatch.setattr(tools, "Client", Client)
    registered = []
    registry = SimpleNamespace(register=registered.append)
    session = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path / "agent"),
        stop=threading.Event(),
        state={},
    )
    tools.register(session, registry, tmp_path)
    tool = next(
        t for t in registered if t.definition["function"]["name"] == "program_events"
    )
    shown = tool.execute({"job_id": "j"})
    assert [ref["metadata"]["sampled_at"] for ref in shown["_images"]] == [2, 3, 4, 5]
    assert shown["cursor"] == 12
    assert len(shown["events"]) == 12
