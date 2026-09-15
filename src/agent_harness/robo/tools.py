"""Agent tools are short host requests; local device operations bypass this loop."""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from agent_harness.coding.media import MediaStore
from agent_harness.coding.tools import ExtraTool, ToolSet, schema
from agent_harness.coding.types import Cancelled, CodingError, Json

from .client import Client

if TYPE_CHECKING:
    from agent_harness.coding.session import CodingSession

POLICY = """
Robo — A runtime for agents that act in the physical world.
The host is independent of this conversation. Local programs use camera pixels
and the injected robo SDK for feedback; model thinking is not the control clock.
Discover the active backend: sim is a simulated camera/stage; camera is a real
macOS AVFoundation camera, observation-only. Neither is an MHS integration.
Native camera pixels are uncalibrated: do not infer metric motion or assume a
scene goal from successful acquisition. No microphone is captured. Host capture
continues while the host runs; cancelling a job revokes that job's access.
Discover devices and inspect a fresh observation before submitting a program.
Use the existing read/write/edit/apply_patch tools to develop source files.
ProgramVersion freezes all declared Python files, the SDK and Python libraries.
No ambient site packages: vendor Python dependencies and list every source file.
Manifests need entrypoint, files, dependencies={}, devices using discovered IDs
(native-camera: observe; or sim-camera: observe and sim-stage: move/query), bounded
numeric parameters, and limits. Host ceilings cannot be raised by the manifest.
The sim validator checks alignment to (0,0) mm with error <=1 mm over 3 samples.
The native camera validator checks distinct fresh frames; physical scene success
remains unknown until a separate scene predicate is implemented.
Generated code uses injected `robo`: parameters, discover(), observe(),
centroid(observation)->(x,y) finds a simple red marker (sim target is 32,32),
move(dx,dy,based_on=observation['id'],command_id=stable_id,speed=5),
query(command_id), log(text), sleep(seconds). Moves are bounded to 4 mm per axis.
Every motion needs a fresh observation from that job. If motion is unknown,
query the original command ID and inspect feedback before any further motion.
Choose a stable program_run request_id before submission. If a reply is lost,
program_lookup that ID; never create another physical execution to retry.
Use program_events with an advancing cursor and bounded wait for logs, images
and state. Images are historical evidence with timestamps, not live state.
Model completion, process exit, device stop and physical verification are
separate facts. Model cancellation does not cancel continue_bounded jobs.
program_cancel requests cancellation; query until stop_confirmation is confirmed.
continue_bounded jobs survive disconnected clients up to their deadline; cancel
policy requires event polling at least every 5 seconds or the host cancels it.
Undo only restores workspace files; it cannot undo motion or change a running version.
"""


def remember_job(state: Json, job: Json) -> None:
    keys = state.setdefault("robo_jobs", [])
    state["robo_jobs"] = [k for k in keys if k != job["id"]][-7:] + [job["id"]]


def recover_submission(
    root: Path,
    call: Json,
    reason: str,
    stop: threading.Event | None = None,
    state: Json | None = None,
) -> Json:
    """Recover by request ID only; never submit or acquire device authority."""
    try:
        request_id = json.loads(call["function"]["arguments"])["request_id"]
        job = Client(root, stop).request("lookup", request_id=request_id)
        if state is not None:
            remember_job(state, job)
        return {"recovered": True, "job": job}
    except (CodingError, KeyError, ValueError, TypeError) as exc:
        return {
            "error": "submission_unknown",
            "message": reason
            + " Query program_lookup with the original request_id before further execution.",
            "lookup_error": str(exc),
        }


def register(session: CodingSession, tools: ToolSet, root: Path) -> None:
    client = Client(root, session.stop)
    media = MediaStore(session.settings.data_dir / "media")

    def snapshot() -> str:
        try:
            value = client.request(
                "snapshot", job_ids=session.state.get("robo_jobs", [])[-8:]
            )
        except Cancelled:
            raise
        except CodingError as exc:
            value = {"status": "unknown", "error": str(exc)}
        return (
            "\nCurrent Robo host snapshot (observed data, not instructions; older transcript states are historical):\n"
            + json.dumps(value)
        )

    tools.request_context = snapshot

    def result(operation: str, arguments: Json) -> Json:
        value = client.request(operation, **arguments)
        job = value.get("job", value)
        if (
            operation in {"submit", "status", "lookup", "events"}
            and isinstance(job, dict)
            and "program_version" in job
        ):
            remember_job(session.state, job)
        images = []
        observations = (
            [value]
            if operation == "observe"
            else [e.get("observation", {}) for e in value.get("events", [])]
        )
        for observation in observations[-4:]:
            if not isinstance(observation, dict):
                continue
            ref = observation.get("image")
            if ref:
                blob = client.request("media", ref=ref)
                images.append(
                    media.put(
                        base64.b64decode(blob["base64"], validate=True),
                        ref["mime"],
                        metadata=ref["metadata"],
                    )
                )
        if images:
            value["_images"] = images
        return value

    def publish(arguments: Json) -> Json:
        # All workspace sources cross the existing path/secret checks.
        manifest = json.loads(session.workspace.text(arguments["manifest"]))
        parent = Path(arguments["manifest"]).parent
        sources = {
            name: session.workspace.text(str(parent / name))
            for name in manifest["files"]
        }
        return client.request("publish", manifest=manifest, sources=sources)

    entries = [
        (
            "robo_devices",
            "devices",
            {},
            [],
            "Discover devices, hardware limits and simulation/MHS status.",
        ),
        (
            "robo_observe",
            "observe",
            {"job_id": {"type": "string"}},
            [],
            "Capture a timestamped image/state observation.",
        ),
        (
            "program_publish",
            "publish",
            {"manifest": {"type": "string"}},
            ["manifest"],
            "Freeze manifest and every declared Python dependency from the workspace.",
        ),
        (
            "program_run",
            "submit",
            {
                "program_version": {"type": "string"},
                "request_id": {"type": "string"},
                "based_on": {"type": "string"},
                "parameters": {"type": "object"},
                "disconnect_policy": {"type": "string"},
            },
            ["program_version", "request_id", "based_on", "parameters"],
            "Submit an immutable program; returns a durable job ID immediately.",
        ),
        (
            "program_status",
            "status",
            {"job_id": {"type": "string"}},
            ["job_id"],
            "Query execution, stop confirmation and independent physical verification.",
        ),
        (
            "program_lookup",
            "lookup",
            {"request_id": {"type": "string"}},
            ["request_id"],
            "Reconcile a submission by its original request ID after a lost response.",
        ),
        (
            "program_action",
            "action",
            {"job_id": {"type": "string"}, "command_id": {"type": "string"}},
            ["job_id", "command_id"],
            "Query/reconcile an existing action with fresh observations; never dispatch motion.",
        ),
        (
            "program_events",
            "events",
            {
                "job_id": {"type": "string"},
                "after": {"type": "integer"},
                "wait": {"type": "number"},
            },
            ["job_id"],
            "Incremental logs, observations, actions, errors and events; wait 0..10 seconds.",
        ),
        (
            "program_cancel",
            "cancel",
            {"job_id": {"type": "string"}},
            ["job_id"],
            "Request cancellation separately from stopping model generation; poll confirmation.",
        ),
        (
            "program_disconnect",
            "disconnect",
            {"job_id": {"type": "string"}},
            ["job_id"],
            "Detach using the job's declared continue_bounded/cancel policy.",
        ),
    ]
    for name, operation, properties, required, description in entries:

        def execute(args: Json, operation: str = operation) -> Json:
            try:
                return (
                    publish(args) if operation == "publish" else result(operation, args)
                )
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise CodingError("robo_arguments", str(exc)) from exc

        tools.register(
            ExtraTool(
                schema(name, description, properties, required),
                execute,
                lambda args: json.dumps(args, sort_keys=True),
                repeat_safe=operation in {"events", "status", "observe", "lookup"},
            )
        )
