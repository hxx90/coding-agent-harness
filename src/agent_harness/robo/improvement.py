"""One bounded improvement attempt, with an operator-owned replay gate.

This module grants no execution or device authority. The caller supplies the
trusted evaluator and confines candidate execution. Transfer cases are never
sent to the proposer; returning a winner does not deploy it on hardware.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Case:
    id: str
    task: str
    split: str
    inputs: dict[str, Any]
    expected: bool
    evidence: str


def fingerprint(value: Any) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, sort_keys=True, allow_nan=False)
    )
    return hashlib.sha256(text.encode()).hexdigest()


def improve_once(
    baseline: str,
    cases: Sequence[Case],
    *,
    propose: Callable[[str, list[dict[str, Any]]], str],
    evaluate: Callable[[str, list[dict[str, Any]]], list[bool]],
    max_added_lines: int = 12,
    max_source_bytes: int = 8192,
) -> tuple[str, dict[str, Any]]:
    """Propose once; require development AND cross-task gains without regressions.

    Labels and case identity stay outside candidate execution. All cases count;
    exceptions, invalid predictions, no-ops and excessive growth fail closed.
    Budget enforcement inside propose/evaluate belongs to their trusted caller.
    """
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError("Cases must have unique, nonempty identities")
    if any(
        not c.id
        or not c.task
        or not c.evidence
        or type(c.expected) is not bool
        or c.split not in {"development", "transfer"}
        for c in cases
    ):
        raise ValueError("Each case needs a task, split, boolean label and evidence")
    dev = {c.task for c in cases if c.split == "development"}
    transfer = {c.task for c in cases if c.split == "transfer"}
    if not dev or not transfer or dev & transfer:
        raise ValueError("Development and transfer must contain distinct tasks")
    if max_added_lines < 0 or max_source_bytes <= 0:
        raise ValueError("Invalid source budget")

    # Snapshot inputs: neither a callback nor a mutable caller can rewrite labels.
    rows = json.loads(json.dumps([vars(c) for c in cases], allow_nan=False))
    report: dict[str, Any] = {
        "scope": "offline_replay_only",
        "accepted": False,
        "baseline_sha256": fingerprint(baseline),
        "suite_sha256": fingerprint(rows),
        "selected_sha256": fingerprint(baseline),
        "limits": {
            "max_added_lines": max_added_lines,
            "max_source_bytes": max_source_bytes,
        },
        "reason": "not_evaluated",
    }

    def predictions(source: str) -> list[bool]:
        inputs = json.loads(json.dumps([r["inputs"] for r in rows]))
        values = evaluate(source, inputs)
        if (
            not isinstance(values, list)
            or len(values) != len(rows)
            or any(type(v) is not bool for v in values)
        ):
            raise ValueError("Evaluator must return one boolean per case")
        return values

    before = predictions(baseline)  # A broken baseline/suite is an operator error.
    development = [
        dict(r, baseline_prediction=p)
        for r, p in zip(rows, before)
        if r["split"] == "development"
    ]
    try:
        candidate = propose(baseline, json.loads(json.dumps(development)))
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("Proposer must return Python source")
        report["candidate_sha256"] = fingerprint(candidate)
        report["added_lines"] = len(candidate.splitlines()) - len(baseline.splitlines())
        if (
            len(candidate.encode()) > max_source_bytes
            or report["added_lines"] > max_added_lines
        ):
            report["reason"] = "source_budget"
            return baseline, report
        compile(candidate, "<candidate>", "exec")
        after = predictions(candidate)
    except Exception as exc:  # noqa: BLE001 - an unsuccessful attempt never promotes
        # Do not leak paths, credentials or provider response bodies into reports.
        report.update(reason="candidate_error", error_type=type(exc).__name__)
        return baseline, report

    report["cases"] = [
        {k: r[k] for k in ("id", "task", "split", "expected", "evidence")}
        | {"baseline": b, "candidate": a}
        for r, b, a in zip(rows, before, after)
    ]
    gains = {
        split: sum(
            b != r["expected"] and a == r["expected"]
            for r, b, a in zip(rows, before, after)
            if r["split"] == split
        )
        for split in ("development", "transfer")
    }
    regressions = [
        r["id"]
        for r, b, a in zip(rows, before, after)
        if b == r["expected"] and a != r["expected"]
    ]
    report.update(gains=gains, regressions=regressions)
    if before == after:
        report["reason"] = "unchanged_behavior"
    elif regressions:
        report["reason"] = "regression"
    elif not all(gains.values()):
        report["reason"] = "missing_development_or_transfer_gain"
    else:
        report.update(accepted=True, reason="improved_without_regression")
    winner = candidate if report["accepted"] else baseline
    report["selected_sha256"] = fingerprint(winner)
    return winner, report
