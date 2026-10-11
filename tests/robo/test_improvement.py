from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_harness.robo.improvement import Case, fingerprint, improve_once


@pytest.fixture
def cases():
    return [
        Case("dev-bad", "restack", "development", {"value": 1}, True, "recording:1"),
        Case(
            "dev-control", "restack", "development", {"value": 0}, False, "recording:2"
        ),
        Case("transfer-bad", "lift", "transfer", {"value": 2}, True, "recording:3"),
        Case(
            "transfer-control", "lift", "transfer", {"value": 0}, False, "recording:4"
        ),
    ]


def evaluator(source, inputs):
    # Trusted test doubles. Real example confines Python in a separate process.
    return {
        "baseline": [False, False, False, False],
        "fixed": [True, False, True, False],
        "overfit": [True, False, False, False],
        "regressed": [True, True, True, False],
    }[source.strip()]


def test_promote_only_cross_task_improvement(cases):
    seen = []

    def propose(source, development):
        seen.extend(development)
        return "fixed"

    winner, report = improve_once(
        "baseline", cases, propose=propose, evaluate=evaluator
    )
    assert winner == "fixed" and report["accepted"]
    assert report["gains"] == {"development": 1, "transfer": 1}
    assert len(seen) == 2 and {r["task"] for r in seen} == {"restack"}
    assert report["selected_sha256"] == fingerprint(winner)
    assert report["scope"] == "offline_replay_only"


@pytest.mark.parametrize(
    "candidate,reason",
    [
        ("baseline\n", "unchanged_behavior"),
        ("overfit", "missing_development_or_transfer_gain"),
        ("regressed", "regression"),
        ("not python !", "candidate_error"),
        ("\n" * 20 + "fixed", "source_budget"),
    ],
)
def test_reject(cases, candidate, reason):
    winner, report = improve_once(
        "baseline", cases, propose=lambda *a: candidate, evaluate=evaluator
    )
    assert winner == "baseline" and not report["accepted"]
    assert report["reason"] == reason
    assert report["selected_sha256"] == fingerprint("baseline")


@pytest.mark.parametrize("bad", [[], [True], [True, False, 1, False], None])
def test_invalid_predictions_fail_closed(cases, bad):
    def evaluate(source, inputs):
        return evaluator(source, inputs) if source == "baseline" else bad

    winner, report = improve_once(
        "baseline", cases, propose=lambda *a: "fixed", evaluate=evaluate
    )
    assert winner == "baseline" and report["reason"] == "candidate_error"


def test_mutating_proposer_cannot_rewrite_suite(cases):
    def propose(source, dev):
        dev[0]["inputs"]["value"] = 99
        dev[0]["expected"] = False
        return "fixed"

    def evaluate(source, inputs):
        assert [r["value"] for r in inputs] == [1, 0, 2, 0]
        return evaluator(source, inputs)

    assert improve_once("baseline", cases, propose=propose, evaluate=evaluate)[1][
        "accepted"
    ]
    assert cases[0].inputs == {"value": 1}


def test_failure_does_not_leak_exception_text(cases):
    def propose(*args):
        raise RuntimeError("SECRET")

    _, report = improve_once("baseline", cases, propose=propose, evaluate=evaluator)
    assert report["error_type"] == "RuntimeError" and "SECRET" not in str(report)


@pytest.mark.parametrize(
    "change",
    [
        lambda c: [],
        lambda c: [c[0], c[0], *c[2:]],
        lambda c: [replace(r, task="same") for r in c],
        lambda c: [replace(r, expected=1) for r in c],
        lambda c: [replace(r, evidence="") for r in c],
        lambda c: [replace(r, split="test") for r in c],
    ],
)
def test_invalid_suite_rejected_before_proposal(cases, change):
    with pytest.raises(ValueError):
        improve_once(
            "baseline",
            change(cases),
            propose=lambda *a: pytest.fail(),
            evaluate=evaluator,
        )


@pytest.fixture
def example():
    if sys.platform != "linux":
        pytest.skip("The confined offline example requires Linux Landlock/seccomp")
    path = Path(__file__).parents[2] / "examples/robo_improve/run.py"
    spec = importlib.util.spec_from_file_location("robo_improve_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_confined_replay_and_no_files_network_or_processes(
    example, tmp_path, monkeypatch
):
    monkeypatch.setenv("RSI_TEST_SECRET", "must-not-leak")
    secret = tmp_path / "secret"
    secret.write_text("forbidden")
    source = f"""
def check(sample):
    import os, socket, subprocess
    assert "RSI_TEST_SECRET" not in os.environ
    for action in (lambda: open({str(secret)!r}).read(),
                   lambda: socket.socket(), lambda: subprocess.run(["true"])):
        try:
            action()
        except OSError:
            continue
        raise AssertionError("Confinement failed")
    return sample["value"] > 0
"""
    assert example.evaluate(source, [{"value": 1}, {"value": 0}]) == [True, False]


def test_candidate_cpu_limit(example):
    with pytest.raises(RuntimeError):
        example.evaluate("def check(sample):\n    while True: pass\n", [{}])


def test_promoted_helper_geometry_and_scope(example):
    source = Path(example.__file__).with_name("seat_vacancy.py").read_text()
    before = [-0.05] * 200
    support = [-0.10] * 200
    samples = [
        {
            "before_z": before,
            "after_z": [0.10] * 200,
            "support_z": support,
            "ceiling": 0.0,
        },
        {"before_z": before, "after_z": before, "support_z": support, "ceiling": 0.0},
        {"before_z": [], "after_z": [], "support_z": support, "ceiling": 0.0},
        {"before_z": before, "after_z": [], "support_z": [], "ceiling": 0.0},
    ]
    assert example.evaluate(source, samples) == [True, False, False, False]
    # No fixed coordinate answer: translating the entire geometry changes nothing.
    translated = [
        {
            k: [z + 0.37 for z in v] if k.endswith("_z") else v + 0.37
            for k, v in s.items()
        }
        for s in samples
    ]
    assert example.evaluate(source, translated) == [True, False, False, False]
    # A displaced/dropped object can also vacate its seat: NOT a retention test.
    assert example.evaluate(source, [dict(samples[0], after_z=[])]) == [True]


def test_oversized_source_rejected_before_execution(cases):
    _, report = improve_once(
        "baseline", cases, propose=lambda *a: "x" * 8193, evaluate=evaluator
    )
    assert report["reason"] == "source_budget"


def test_broken_baseline_is_not_a_candidate_failure(cases):
    with pytest.raises(ValueError, match="one boolean"):
        improve_once(
            "baseline", cases, propose=lambda *a: pytest.fail(), evaluate=lambda *a: []
        )
