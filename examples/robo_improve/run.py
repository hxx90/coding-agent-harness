"""Bounded, offline capability improvement; never opens a robot connection."""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from agent_harness.robo.improvement import Case, improve_once


def worker() -> None:
    from agent_harness.robo.linux import restrict

    payload = json.load(sys.stdin)
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
    restrict([])  # Fail closed: no files, devices, networking or child processes.
    scope: dict = {}
    exec(compile(payload["source"], "<candidate>", "exec"), scope)  # noqa: S102 - confined child
    results = [scope["check"](sample) for sample in payload["inputs"]]
    print(json.dumps(results, allow_nan=False))


def evaluate(source: str, inputs: list[dict]) -> list[bool]:
    # No labels/IDs, provider secrets or host file handles enter this process.
    env = {
        k: os.environ[k]
        for k in ("PATH", "PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH")
        if k in os.environ
    }
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            input=json.dumps({"source": source, "inputs": inputs}).encode(),
            stdout=output,
            stderr=errors,
            env=env,
            timeout=15,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Confined replay failed")
        output.seek(0)
        return json.loads(output.read(1024**2))


def model_proposer(directory: Path, *, url: str, model: str, api_key: str):
    from agent_harness.coding.session import CodingSession
    from agent_harness.coding.settings import ProviderSettings, Settings

    def propose(source: str, development: list[dict]) -> str:
        workspace = directory / "proposal"
        workspace.mkdir()
        (workspace / "candidate.py").write_text(source)
        # Compact empirical evidence, not megabytes of repeated point arrays.
        summaries = []
        for row in development:
            ceiling = row["inputs"]["ceiling"]
            summary = {k: row[k] for k in ("expected", "baseline_prediction")}
            summary["ceiling"] = ceiling
            summary["heights"] = {
                k: {
                    "count": len(v),
                    "min": min(v, default=None),
                    "max": max(v, default=None),
                    "at_or_below_ceiling": sum(z <= ceiling for z in v),
                }
                for k, v in row["inputs"].items()
                if k.endswith("_z")
            }
            summaries.append(summary)
        task = (
            "Repair candidate.py's reusable old-seat vacancy check from the development evidence. "
            "Inputs before_z/after_z/support_z are observed point heights in the SAME XY region; "
            "ceiling is the upper boundary of the old seat in the same frame/metres. "
            "check(sample) must return bool. An elevated object can remain at the same XY. "
            "Vacancy does NOT imply retention or successful grasp; do not broaden the claim. "
            "Keep the original support/occupancy thresholds; fix geometric selection only. "
            "Use standard Python only, no imports, no hardcoded task IDs or coordinates. "
            "At most 12 additional lines, no new files. Read, edit, then finish. "
            "No test tool is configured: do not claim validation. External replay follows.\n"
            + json.dumps(summaries)
        )
        settings = Settings(
            project=workspace,
            data_dir=directory / "session",
            provider=ProviderSettings(
                base_url=url,
                model=model,
                api_key=api_key,
                stream=False,
                max_output_tokens=4096,
                timeout=90,
            ),
            max_turns=8,
            max_tokens=40000,
            max_seconds=180,
            context_chars=24000,
            editable=("candidate.py",),
            permission_rules=(
                {"tool": "*", "action": "deny"},
                *({"tool": t, "action": "allow"} for t in ("read", "edit", "write")),
            ),
        )
        result = CodingSession(settings).run(task)
        (directory / "proposer-result.json").write_text(
            json.dumps(asdict(result), indent=2)
        )
        if result.status != "completed":
            raise RuntimeError("Proposer did not complete within its budget")
        return (workspace / "candidate.py").read_text()

    return propose


def run(
    cases_path: Path,
    output: Path,
    *,
    candidate: Path | None = None,
    url: str = "",
    model: str = "",
    api_key: str = "",
) -> dict:
    payload = json.loads(cases_path.read_text())
    cases = [Case(**row) for row in payload["cases"]]
    baseline = Path(__file__).with_name("baseline.py").read_text()
    output.mkdir(parents=True, exist_ok=False)  # Never overwrite an earlier attempt.
    if candidate is None and not (url and model and api_key):
        raise ValueError(
            "Supply a candidate or an explicitly configured model connection"
        )
    propose = (
        (lambda source, dev: candidate.read_text())
        if candidate
        else model_proposer(
            output,
            url=url,
            model=model,
            api_key=api_key,
        )
    )
    selected, report = improve_once(baseline, cases, propose=propose, evaluate=evaluate)
    # Negative control on the same frozen suite. No model call or new data.
    _, noop = improve_once(
        baseline,
        cases,
        evaluate=evaluate,
        propose=lambda source, dev: source.replace("max(40,", "max(41,"),
    )
    report["negative_control"] = noop
    report["provenance"] = payload["provenance"]
    report["deployment"] = (
        "none; selected source is an offline capability candidate only"
    )
    (output / "selected.py").write_text(selected)
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--candidate", type=Path, help="Replay an existing proposal, without a model"
    )
    parser.add_argument("--model-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-key-env", default="AGENT_HARNESS_API_KEY")
    args = parser.parse_args()
    if args.worker:
        worker()
        return
    if args.cases is None or args.output is None:
        parser.error("--cases and --output are required")
    result = run(
        args.cases,
        args.output,
        candidate=args.candidate,
        url=args.model_url,
        model=args.model,
        api_key=os.environ.get(args.api_key_env, ""),
    )
    print(json.dumps({k: result[k] for k in ("accepted", "reason", "scope")}))


if __name__ == "__main__":
    main()
