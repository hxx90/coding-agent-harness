"""Export audited CaP-X point heights, without running a model or simulator.

Benchmark-specific selection stays in this example, not in the learning gate.
Large recordings/derived arrays remain external artifacts rather than git data.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

from agent_harness.robo.wire import decode

# The already-audited second restack job's pure geometry (not its robot program).
GEOMETRY_SHA = "0e413eb5bb5ef55425a775bac72f122eaa02207de8bae01ecf1a118dda67cc86"


def objects(db, kind):
    return [
        json.loads(row[0])
        for row in db.execute(
            "select body from objects where kind=? order by rowid", (kind,)
        )
    ]


def item(db, kind, key):
    row = db.execute(
        "select body from objects where kind=? and id=?", (kind, key)
    ).fetchone()
    if row is None:
        raise ValueError(f"Missing {kind}: {key}")
    return json.loads(row[0])


def geometry(source):
    if hashlib.sha256(source.encode()).hexdigest() != GEOMETRY_SHA:
        raise ValueError("Unexpected archived geometry; review before executing it")
    names = {"project_points", "colors", "rect_angle", "colored", "cube", "column"}
    nodes = [
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name in names
    ]
    if {n.name for n in nodes} != names or any(n.decorator_list for n in nodes):
        raise ValueError("Unexpected geometry definitions")

    def require(ok, *args, **kwargs):
        if not ok:
            raise ValueError("Recorded geometry is not observable")

    scope = {"np": np, "require": require}
    exec(  # noqa: S102 - pinned audited source
        compile(ast.Module(body=nodes, type_ignores=[]), "<audited-geometry>", "exec"),
        scope,
    )
    return scope


def recordings(trial, *, restack=False):
    grade = json.loads((trial / "grade.json").read_text())
    if grade.get("task_completed") is not True or grade.get("infra_errors"):
        raise ValueError("Expected the independently graded successful recording")
    with sqlite3.connect(
        (trial / "host/robo.sqlite3").as_uri() + "?mode=ro", uri=True
    ) as db:
        jobs = objects(db, "job")
        if len(jobs) != (3 if restack else 1) or any(
            j["status"] != "exited" or j.get("exit_code") != 0 for j in jobs
        ):
            raise ValueError("Expected the finalized audited trial")
        job = jobs[1] if restack else jobs[0]
        actions = [a for a in objects(db, "action") if a["job_id"] == job["id"]]
        if not all(a["status"] == "confirmed" for a in actions):
            raise ValueError("Unconfirmed action in recording")
        captures = [a for a in actions if a["device_operation"] == "get_observation"]
        if len(captures) != 3:
            raise ValueError("Unexpected observation sequence")
        if restack:
            report = job["latest_report"]
            captures = captures[-2:]
        else:
            report = json.loads(
                db.execute(
                    "select body from events where json_extract(body,'$.stage')='grasp_red'"
                ).fetchone()[0]
            )
            if any(
                a["device_operation"] != "get_observation"
                for a in actions
                if a["timestamp"] > captures[1]["timestamp"]
            ):
                raise ValueError("Unexpected later movement in lift recording")
            captures = captures[:2]
        references = [item(db, "observation", r["id"]) for r in report["observations"]]
        source = item(db, "program", job["program_version"])["sources"][
            "specialist_common.py"
        ]
    if len(references) != 2:
        raise ValueError("Expected a before/after observation pair")
    frames = []
    for action, observation in zip(captures, references):
        camera = decode(action["result"])["robot0_robotview"]
        path = trial / "host/media" / observation["image"]["id"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != observation["image"]["id"]:
            raise ValueError("Image hash mismatch")
        if not np.array_equal(camera["images"]["rgb"], np.asarray(Image.open(path))):
            raise ValueError("RGB-D does not match the referenced image")
        frames.append(
            {
                "rgb": camera["images"]["rgb"],
                "depth": np.asarray(camera["images"]["depth"], float).squeeze(),
                "K": np.asarray(camera["intrinsics"], float),
                "T": np.asarray(camera["pose_mat"], float),
                "id": observation["id"],
            }
        )
    evidence = {
        "trial": str(trial),
        "program_version": job["program_version"],
        "actions": [a["id"] for a in captures],
        "images": [o["image"]["id"] for o in references],
    }
    return source, frames, evidence


def export(root: Path, output: Path) -> dict:
    root = root.resolve()
    source, _, _ = recordings(root / "cube_restack/trial_01", restack=True)
    functions = geometry(source)
    cases, provenance = [], []
    trials = [("cube_restack", 1, "development")]
    trials += [("cube_lifting", seed, "transfer") for seed in range(1, 11)]
    for task, seed, split in trials:
        trial = root / task / f"trial_{seed:02d}"
        _, (before, after), evidence = recordings(trial, restack=task == "cube_restack")
        obj = functions["cube"](before, "red")
        ceiling = float(obj["top"] + 0.02)

        def heights(frame, color, xy=obj["xy"]):
            return functions["column"](frame, color, xy)[:, 2].tolist()

        initial = heights(before, "red")
        for label, frame, expected in (
            ("before_action_control", before, False),
            ("recorded_vacated_seat", after, True),
        ):
            cases.append(
                {
                    "id": f"{task}/{seed}/{label}",
                    "task": task,
                    "split": split,
                    "expected": expected,
                    "inputs": {
                        "before_z": initial,
                        "after_z": heights(frame, "red"),
                        "support_z": heights(frame, "white"),
                        "ceiling": ceiling,
                    },
                    "evidence": json.dumps(evidence, sort_keys=True),
                }
            )
        provenance.append(evidence)
    payload = {
        "cases": cases,
        "provenance": {
            "geometry_sha256": GEOMETRY_SHA,
            "recordings": provenance,
            "limitation": "Previously inspected development recordings, NOT held-out trials. "
            "Controls are before-action frames, NOT failed grasp attempts. This labels old-seat "
            "vacancy only, never retention. No hardware execution or new benchmark score.",
        },
    }
    with output.open("x") as stream:
        json.dump(payload, stream, allow_nan=False)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = export(args.recording_root, args.output)
    print(
        json.dumps({"cases": len(result["cases"]), "bytes": args.output.stat().st_size})
    )
