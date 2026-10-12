from __future__ import annotations

import copy
from contextlib import closing

import pytest

from agent_harness.coding.types import CodingError
from agent_harness.robo.demo import MANIFEST
from agent_harness.robo.programs import Programs, parameter_ranges
from agent_harness.robo.runtime import Runtime
from agent_harness.robo.store import Store


@pytest.mark.parametrize(
    "definitions",
    [
        None,
        [],
        0,
        True,
        "gain",
        {"gain": 0.5},
        {"gain": None},
        {"gain": []},
        {"gain": {}},
        {"gain": {"min": 0}},
        {"gain": {"max": 1}},
        {7: {"min": 0, "max": 1}},
        *(
            {"gain": {"min": low, "max": high}}
            for low, high in (
                (True, 1),
                (0, False),
                (None, 1),
                (0, "1"),
                (float("nan"), 1),
                (0, float("nan")),
                (float("-inf"), 1),
                (0, float("inf")),
                (-1000001, 1),
                (0, 1000001),
                (2, 1),
                (0, 10**400),
            )
        ),
    ],
)
def test_bad_parameter_definitions_fail_before_runtime_freeze(
    tmp_path,
    monkeypatch,
    definitions,
):
    store = Store(tmp_path)
    programs = Programs(store)

    def unexpected_freeze(_):
        pytest.fail("Invalid manifest must be rejected before freezing dependencies")

    monkeypatch.setattr(programs, "_runtime", unexpected_freeze)
    manifest = {**copy.deepcopy(MANIFEST), "parameters": definitions}
    try:
        with pytest.raises(CodingError) as caught:
            programs.publish({"manifest": manifest, "sources": {"control.py": "pass"}})
        assert caught.value.code == "invalid_program"
        assert store.all("program") == []
        assert not (tmp_path / "programs").exists()
        assert not (tmp_path / "runtimes").exists()
    finally:
        store.db.close()


def test_parameter_ranges_are_inclusive_and_do_not_rewrite_metadata():
    value = {
        "gain": {"min": -1e6, "max": 1e6, "description": "signed gain"},
        "fixed": {"min": 0.25, "max": 0.25},
    }
    original = copy.deepcopy(value)
    assert parameter_ranges(value) == {"gain": (-1e6, 1e6), "fixed": (0.25, 0.25)}
    assert value == original
    assert parameter_ranges({}) == {}


@pytest.mark.parametrize("parameters", [None, [], "gain", {}, {"other": 0.5}])
def test_submission_requires_exact_parameter_object_before_any_job(
    tmp_path, parameters
):
    with closing(Runtime(tmp_path)) as runtime:
        runtime.store.put(
            "program", "unlaunched", {"manifest": copy.deepcopy(MANIFEST)}
        )
        with pytest.raises(CodingError) as caught:
            runtime.request(
                "submit",
                {
                    "program_version": "unlaunched",
                    "request_id": "bad-values",
                    "parameters": parameters,
                    "based_on": "not-needed-before-validation",
                },
            )
        assert caught.value.code == "invalid_parameters"
        for kind in ("job", "request", "action"):
            assert runtime.store.all(kind) == []


def test_legacy_bad_definition_is_rejected_before_admission(tmp_path):
    with closing(Runtime(tmp_path)) as runtime:
        manifest = {**copy.deepcopy(MANIFEST), "parameters": {"gain": 0.65}}
        runtime.store.put("program", "old-malformed", {"manifest": manifest})
        with pytest.raises(CodingError) as caught:
            runtime.request(
                "submit",
                {
                    "program_version": "old-malformed",
                    "request_id": "old-version",
                    "parameters": {"gain": 0.65},
                    "based_on": "not-needed-before-validation",
                },
            )
        assert caught.value.code == "invalid_program"
        for kind in ("job", "request", "action"):
            assert runtime.store.all(kind) == []
