from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Literal, cast

from vibe.core.config import build_default_orchestrator, load_dotenv_values
from vibe.core.config.harness_files import init_harness_files_manager
from vibe.core.hardware import (
    HardwarePolicyError,
    HardwarePolicyRunner,
    HardwareRuntime,
)
from vibe.core.hardware._geometry import H1_BUNDLE_SHA256, H1_VERSION
from vibe.core.llm.backend.factory import create_backend
from vibe.core.types import LLMMessage, Role
from vibe.hardware_sim.adapter import MUJOCO_PANDA_DEVICE_ID, RobosuiteSimulatorAdapter

_SYSTEM = """Generate only a fenced Python policy for Robo Lift. The operator has connected,
leased and armed the SIMULATOR. Do not import, define functions, use attributes, slices,
comprehensions, exceptions, or access files/network. You can assign names, index dict/list,
use if/for/while/break, arithmetic, abs/min/max/len/range/round, print.
Calls: observe() returns snapshot with state; move_cartesian(delta_xyz=[x,y,z], steps=N);
gripper_open(); gripper_close(); verify("lift_object"); stop().
The frozen budget is 200 statements, 50 actions, 50 total loop iterations, 120 seconds.
Generate one program, with no external retries. Use only the supplied camera perception
and robot proprioception. Target the red cube; no cube ground truth is available.
state.perception contains max 128 metric XYZ points and their centroid, ok/count;
world axes z up. Check ok before using geometry. A missing cluster means occlusion.
End effector position is in the same world frame. move_cartesian values are normalized
per-step increments (about 0.05 meters per unit), not absolute coordinates; clamp to [-1,1].
Use a compact closed loop (e.g. gain 8, steps=5) to align XY above the cube, descend,
close the gripper and lift by about 0.12 m. Reobserve after actions. Keep enough budget
for final verify. Only the independent verifier determines success. Do not call home,
as the paired initial state is fixed. The manifest states any additional allowed
read-only helper and its exact documentation. If absent, use the shared visual centroid.
"""


_COMPACT_CONTRACT = """
Output contract v2: return <=40 lines of executable Python, no comments or prose.
Comparisons supported: == != < <= > >= in not in. `is` / `is not` are unsupported;
avoid None sentinels. Budget counts EACH executed assignment, if, loop, action,
observe and print, including inside loops. Use <=8 statements per loop body,
<=7 iterations per phase; reuse variables and inline clamp/error arithmetic.
Keep only two positioning phases (XY, then XYZ descent), then gripper_close,
a short upward move, observe, verify. Do not write a separate verification loop.
Use a boolean for availability and initialize the target only from a valid visual
cluster. If it becomes occluded, stop the positioning loop. Finish with verify.
"""


def _save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


async def _trial(  # noqa: PLR0914, PLR0915
    config: Any,
    *,
    directory: Path,
    seed: int,
    library: Literal["off", "h1-v8-v9"],
    viewer: bool,
    prompt_version: str,
) -> dict[str, Any]:
    import numpy as np

    directory.mkdir(parents=True)
    adapter = RobosuiteSimulatorAdapter(
        show_viewer=viewer,
        observation_mode="rgbd",
        geometry_library=library,
        seed=seed,
        evidence_dir=directory / "evidence",
        recording_dir=directory / "recordings",
    )
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=directory)
    device = MUJOCO_PANDA_DEVICE_ID
    run_id = f"lift-{seed}-{library}"
    row: dict[str, Any] = {
        "seed": seed,
        "library": library,
        "status": "error",
        "requests": 0,
        "helper_calls": 0,
    }
    system = _SYSTEM + (_COMPACT_CONTRACT if prompt_version == "v2" else "")
    row["prompt_version"] = prompt_version
    start = time.monotonic()
    try:
        manifest = (await runtime.discover())[0]
        snapshot = await runtime.connect(device, run_id=run_id)
        # State hash is evaluator-only and never included in the model request.
        assert adapter._environment is not None
        initial = np.asarray(
            adapter._environment.sim.get_state().flatten(), dtype="<f8"
        )
        row["initial_state_sha256"] = hashlib.sha256(initial.tobytes()).hexdigest()
        row["initial_points_sha256"] = hashlib.sha256(
            json.dumps(
                cast(dict[str, Any], snapshot.state["perception"])["points"]
            ).encode()
        ).hexdigest()
        prompt = (
            "Lift the red cube and verify it.\nManifest:\n"
            + manifest.model_dump_json()
            + "\nInitial observation:\n"
            + snapshot.model_dump_json()
        )
        _save(directory / "request.json", {"system": system, "user": prompt})
        model = config.get_active_model()
        provider = config.get_provider_for_model(model)
        row["model"] = model.name
        backend = create_backend(
            provider=provider, timeout=90, retry_max_elapsed_time=0
        )
        row["requests"] = 1
        async with backend:
            response = await backend.complete(
                model=model,
                messages=[
                    LLMMessage(role=Role.system, content=system),
                    LLMMessage(role=Role.user, content=prompt),
                ],
                temperature=0.0,
                tools=None,
                tool_choice=None,
                max_tokens=4096,
                extra_headers=None,
            )
        row["finish_reason"] = response.stop.reason if response.stop else None
        row["usage"] = response.usage.model_dump() if response.usage else None
        content = response.message.content or ""
        (directory / "response.txt").write_text(content)
        match = re.fullmatch(
            r"\s*```(?:python)?\s*\n(.*?)\n```\s*", content, flags=re.DOTALL
        )
        source = match.group(1) if match else content
        (directory / "generated.py").write_text(source)
        lease = await runtime.acquire_lease(
            device, owner="paired-lift-smoke", run_id=run_id
        )
        await runtime.arm(device, lease_id=lease.lease_id, run_id=run_id)
        runner = HardwarePolicyRunner(runtime, artifact_dir=directory / "policies")
        try:
            result = await runner.run(
                source, device_id=device, lease_id=lease.lease_id, run_id=run_id
            )
            row["policy_outcome"] = result.task_outcome
        except HardwarePolicyError as error:
            row["error_code"] = error.code
            row["error_message"] = str(error)
        report = await runtime.verify(device, criterion="lift_object", run_id=run_id)
        _save(
            directory / "independent-verification.json", report.model_dump(mode="json")
        )
        row["status"] = (
            "timeout"
            if row.get("error_code") == "policy_timeout"
            else report.status.value
        )
        for path in (directory / "policies").glob("*.json"):
            policy = json.loads(path.read_text())
            row["helper_calls"] += sum(
                step["kind"] == "capability" for step in policy["steps"]
            )
        row["stop_acknowledged"] = (
            await runtime.stop(device, reason="smoke finished", run_id=run_id)
        ).acknowledged
        row["recording_uri"] = (await runtime.observe(device, run_id=run_id)).state[
            "recording_uri"
        ]
        await runtime.release_lease(device, lease_id=lease.lease_id, run_id=run_id)
    except Exception as error:
        # Never serialize transport errors, which can contain credentials or URLs.
        row["error_type"] = type(error).__name__
    finally:
        await runtime.aclose()
        row["elapsed_seconds"] = round(time.monotonic() - start, 3)
        _save(directory / "result.json", row)
    return row


async def run(args: argparse.Namespace) -> None:
    init_harness_files_manager("user", "project")
    load_dotenv_values()
    config = (await build_default_orchestrator()).config
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    seeds = list(range(args.seed, args.seed + args.pairs))
    _save(
        root / "protocol.json",
        {
            "scope": "engineering smoke; not statistical replication",
            "seeds": seeds,
            "model": config.get_active_model().name,
            "temperature": 0,
            "max_output_tokens": 4096,
            "requests_per_trial": 1,
            "max_statements": 200,
            "max_actions": 50,
            "max_loop_iterations": 50,
            "policy_timeout_seconds": 120,
            "preprocessing": "red-rgbd-v1",
            "bundle_sha256": H1_BUNDLE_SHA256,
            "controller": "OSC_POSE; same prompt; model-generated code may differ",
            "viewer": args.viewer,
            "prompt_version": args.prompt_version,
            "stopping_rule": "no retry or seed replacement; stop on initial-state mismatch",
        },
    )
    rows = []
    for index, seed in enumerate(seeds):
        versions: list[Literal["off", "h1-v8-v9"]] = (
            ["off", H1_VERSION] if index % 2 == 0 else [H1_VERSION, "off"]
        )
        pair = []
        for library in versions:
            row = await _trial(
                config,
                directory=root / f"{seed}-{library}",
                seed=seed,
                library=library,
                viewer=args.viewer,
                prompt_version=args.prompt_version,
            )
            rows.append(row)
            pair.append(row)
            _save(root / "results.json", rows)
            print(json.dumps(row), flush=True)
        if pair[0].get("initial_state_sha256") != pair[1].get(
            "initial_state_sha256"
        ) or pair[0].get("initial_points_sha256") != pair[1].get(
            "initial_points_sha256"
        ):
            raise RuntimeError(
                "Paired initial state or visual observation mismatch; stopped"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Small paired RGB-D Lift smoke using the configured Robo model"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pairs", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--seed", type=int, default=117)
    parser.add_argument("--viewer", action="store_true")
    parser.add_argument("--prompt-version", choices=("v1", "v2"), default="v1")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
