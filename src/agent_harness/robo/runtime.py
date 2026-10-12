"""Execution supervisor, device authorization, reconciliation and verification."""

from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from agent_harness.coding.media import MediaStore
from agent_harness.coding.types import CodingError, Json

from .hardware import MHS_STATUS, Hardware, SimHardware
from .programs import Programs, parameter_ranges
from .store import Store, canonical, digest
from .wire import MAX_WIRE

TERMINAL = {"exited", "failed", "cancelled", "timed_out", "interrupted"}
DEFAULT_LIMITS = {
    "wall_seconds": 30,
    "cpu_seconds": 10,
    "memory_mb": 512,
    "max_actions": 100,
    "max_events": 500,
    "max_log_bytes": 65536,
    "max_step_mm": 4,
    "max_speed_mm_s": 10,
    "observation_age_s": 2,
    # Admission is not a motion command. Operators can allow model latency
    # here without relaxing the local feedback deadline for any device action.
    "submission_observation_age_s": 2,
}


def resident_bytes(pid: int) -> int:
    """OS RSS, sampled by the host (not a memory reservation)."""
    if sys.platform == "linux":
        try:
            pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE")
        except (FileNotFoundError, ProcessLookupError):
            return 0
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    data = ctypes.create_string_buffer(96)
    length = library.proc_pidinfo(pid, 4, 0, data, len(data))
    if length != 96:
        return 0  # Process may have exited between poll() and this sample.
    return int(ctypes.c_uint64.from_buffer(data, 8).value)


def number(value: object, name: str, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise CodingError(
            "invalid_arguments", f"{name} must be finite and in [{minimum}, {maximum}]"
        )
    return float(value)


class Runtime:
    def __init__(
        self,
        root: Path,
        *,
        backend: str = "sim",
        camera: str | None = None,
        hardware_factory: Callable[[Store, MediaStore], Hardware] | None = None,
        packages: tuple[str, ...] = (),
        limits: Json | None = None,
        max_jobs: int | None = None,
    ) -> None:
        if max_jobs is not None and (type(max_jobs) is not int or max_jobs < 1):
            raise CodingError("invalid_limit", "max_jobs must be a positive integer")
        self.max_jobs = max_jobs
        if hardware_factory is None and backend not in {"sim", "camera"}:
            raise CodingError(
                "mhs_unavailable", MHS_STATUS["reason"], details=MHS_STATUS
            )
        if camera is not None and backend != "camera":
            raise CodingError("invalid_backend", "--camera requires --backend camera")
        self.limits = dict(DEFAULT_LIMITS)
        for name, value in (limits or {}).items():
            if name not in self.limits:
                raise CodingError("invalid_limit", f"Unsupported host limit: {name}")
            number(value, name, 0.01, 1e9)
            self.limits[name] = value
        self.store = Store(root)
        self.media = MediaStore(root / "media")
        self.hardware: Hardware
        self.condition = threading.Condition(threading.RLock())
        self.publication_lock = threading.Lock()
        self.workers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.closing = False
        try:
            identities = self.store.all("runtime")
            identity = identities[0] if identities else None
            # Pre-camera stores belong to the original simulated workcell.
            if identity is None and (self.store.all("job") or self.store.all("device")):
                identity = {"backend": "sim", "resource": "sim-workcell"}
            if identity and hardware_factory is None and identity["backend"] != backend:
                raise CodingError(
                    "device_identity",
                    "This home belongs to another backend; use a separate --home",
                )
            if hardware_factory is not None:
                self.hardware = hardware_factory(self.store, self.media)
            elif backend == "camera":
                from .camera import CameraHardware

                self.hardware = CameraHardware(self.store, self.media, camera)
            else:
                self.hardware = SimHardware(self.store, self.media)
            current = {
                "backend": self.hardware.backend,
                "resource": self.hardware.resource,
            }
            if identity and identity != current:
                raise CodingError(
                    "device_identity",
                    "This home belongs to another device; use a separate --home",
                )
            self.store.put("runtime", "identity", current, immutable=True)
            self.programs = Programs(self.store, packages=packages)
            self._recover()
        except BaseException:
            if hasattr(self, "hardware"):
                self.hardware.close()
            self.store.db.close()
            raise

    def _recover(self) -> None:
        # The gateway is fenced on host death: workers possess only old pipes.
        # Restart never resumes a program or dispatches an old command.
        for job in self.store.all("job"):
            if job["status"] not in TERMINAL:
                job.update(
                    status="interrupted",
                    finished_at=time.time(),
                    stop_confirmation=self.hardware.stop(),
                    physical_verification={
                        "status": "unknown",
                        "reason": "host restart; no motion replayed",
                    },
                )
                self.store.put("job", job["id"], job)
                try:
                    self.store.event(
                        job, "host_recovered", observation=self._observe(job)
                    )
                except CodingError as exc:
                    self.store.event(job, "host_recovered", observation_error=str(exc))
            with self.condition:
                self._reconcile(job)

    def _observe(self, job: Json | None = None) -> Json:
        value = self.hardware.observe(job)
        value.pop("pixels", None)
        return value

    def _job(self, key: str) -> Json:
        return self.store.get("job", key)

    def _save(self, job: Json) -> None:
        self.store.put("job", job["id"], job)
        self.condition.notify_all()

    def request(self, op: str, args: Json) -> Json:
        if op == "publish":
            # Freeze files outside the control lock: I/O must not stall action
            # deadlines, cancellation or protection. Publications still serialize.
            with self.publication_lock:
                with self.condition:
                    if self.closing:
                        raise CodingError("host_stopping", "Host is shutting down")
                return self.programs.publish(args)
        with self.condition:
            if op == "health":
                return {
                    "status": "ready",
                    "backend": self.hardware.backend,
                    "simulated": self.hardware.simulated,
                    "protocol": 1,
                    "mhs": MHS_STATUS,
                }
            if op == "devices":
                return {
                    **self.hardware.discover(),
                    "program_dependencies": self.programs.dependencies(),
                    "program_limits": self.limits,
                    "verification": dict(self.hardware.goal),
                    "remaining_jobs": (
                        None
                        if self.max_jobs is None
                        else max(0, self.max_jobs - len(self.store.all("job")))
                    ),
                }
            if op == "snapshot":
                keys = args.get("job_ids", [])
                if not isinstance(keys, list) or len(keys) > 8:
                    raise CodingError(
                        "invalid_arguments", "snapshot accepts up to eight jobs"
                    )
                return {
                    "sampled_at": time.time(),
                    "source": self.hardware.backend + "-adapter-state",
                    "backend": self.hardware.backend,
                    "simulated": self.hardware.simulated,
                    "device_state": dict(self.hardware.state),
                    "jobs": [
                        {
                            "event_cursor": self.store.cursor(key),
                            **{
                                k: v
                                for k, v in self._job(key).items()
                                if k
                                in {
                                    "id",
                                    "program_version",
                                    "status",
                                    "deadline",
                                    "stop_confirmation",
                                    "physical_verification",
                                    "uncertain_action",
                                    "counts",
                                    "latest_report",
                                }
                            },
                        }
                        for key in keys
                    ],
                }
            if op == "observe":
                return self._observe(
                    self._job(args["job_id"]) if args.get("job_id") else None
                )
            if op == "submit":
                return self.submit(args)
            if op == "status":
                return self._job(args["job_id"])
            if op == "action":
                return self._action(self._job(args["job_id"]), "query", args)
            if op == "jobs":
                return {"jobs": self.store.all("job")}
            if op == "lookup":
                return self._job(
                    self.store.get("request", args["request_id"])["job_id"]
                )
            if op == "events":
                return self.events(args)
            if op == "cancel":
                return self.cancel(args["job_id"])
            if op == "disconnect":
                job = self._job(args["job_id"])
                if job["disconnect_policy"] == "cancel":
                    return self.cancel(job["id"])
                self.store.event(job, "client_detached", policy="continue_bounded")
                return job
            if op == "protect":
                confirmation = self.hardware.stop(protect=True)
                for job in self.store.all("job"):
                    if job["status"] not in TERMINAL:
                        self.cancel(job["id"])
                self.store.event(None, "device_protection", confirmation=confirmation)
                return confirmation
            if op == "media":
                return {
                    "base64": base64.b64encode(self.media.get(args["ref"])).decode()
                }
            raise CodingError("invalid_operation", f"Unknown host operation: {op}")

    def submit(self, args: Json) -> Json:
        request_id = args["request_id"]
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,120}", request_id
        ):
            raise CodingError(
                "invalid_request", "A stable 1..120 character request_id is required"
            )
        fingerprint = digest(args)
        try:
            previous = self.store.get("request", request_id)
        except CodingError:
            previous = None
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise CodingError(
                    "request_conflict", "Request ID already has different parameters"
                )
            return self._job(previous["job_id"])
        if self.closing or self.hardware.state["protected"]:
            raise CodingError(
                "unavailable", "Host stopping or device protection latched"
            )
        if self.max_jobs is not None and len(self.store.all("job")) >= self.max_jobs:
            raise CodingError("resource_limit", "Host execution-job budget exhausted")
        if any(
            action["status"] in {"unknown", "dispatching"}
            for action in self.store.all("action")
        ):
            raise CodingError(
                "reconciliation_required",
                "An earlier job has an unresolved action; query it before transferring control",
            )
        if any(
            job["status"] not in TERMINAL
            or job.get("stop_confirmation", {}).get("status") != "confirmed"
            for job in self.store.all("job")
        ):
            raise CodingError(
                "resource_busy",
                self.hardware.resource + " is owned or stop remains unconfirmed",
            )
        program = self.store.get("program", args["program_version"])
        manifest = program["manifest"]
        grants = manifest.get("devices")
        allowed = self.hardware.grants
        if (
            not isinstance(grants, dict)
            or not grants
            or any(
                device not in allowed
                or not isinstance(ops, list)
                or not ops
                or set(ops) - set(allowed[device])
                for device, ops in grants.items()
            )
        ):
            raise CodingError(
                "device_grant",
                "Program must request explicit supported device operations",
            )
        limits = dict(self.limits)
        for key, value in manifest.get("limits", {}).items():
            if key not in limits:
                raise CodingError("invalid_limit", f"Unsupported limit: {key}")
            number(value, key, 0.01, self.limits[key])
            if key in {
                "cpu_seconds",
                "memory_mb",
                "max_actions",
                "max_events",
                "max_log_bytes",
            } and (not isinstance(value, int) or isinstance(value, bool)):
                raise CodingError("invalid_limit", f"{key} must be an integer")
            limits[key] = value
        parameters = args.get("parameters", {})
        definitions = parameter_ranges(manifest.get("parameters", {}))
        if not isinstance(parameters, dict) or set(parameters) != set(definitions):
            raise CodingError(
                "invalid_parameters", "Parameters must exactly match the manifest"
            )
        for key, (low, high) in definitions.items():
            number(parameters[key], key, low, high)
        policy = args.get("disconnect_policy", "continue_bounded")
        if policy not in {"continue_bounded", "cancel"}:
            raise CodingError("invalid_policy", "Use continue_bounded or cancel")
        observation = self.store.get("observation", args["based_on"])
        self._fresh(observation, limits["submission_observation_age_s"])
        now = time.time()
        job: Json = {
            "id": "job_" + uuid.uuid4().hex,
            "request_id": request_id,
            "program_version": program["id"],
            "status": "starting",
            "created_at": now,
            "parameters": parameters,
            "devices": grants,
            "limits": limits,
            "resource": self.hardware.resource,
            "backend": self.hardware.backend,
            "hardware": self.hardware.discover(),
            "based_on": observation["id"],
            "disconnect_policy": policy,
            "client_deadline": now + 5,
            "deadline": now + limits["wall_seconds"],
            "counts": {"actions": 0, "events": 0, "log_bytes": 0},
            "exit_code": None,
            "stop_confirmation": {"status": "pending"},
            "physical_verification": {
                "status": "unknown",
                "reason": "not yet observed after execution",
            },
            "goal": dict(self.hardware.goal),
            "simulated": self.hardware.simulated,
        }
        # Request mapping precedes any worker/device effects. A lost submit reply
        # is resolved with lookup(request_id), never by inventing another ID.
        self.store.create_job(job, fingerprint)
        self.store.event(job, "submitted")
        stop = threading.Event()
        thread = threading.Thread(
            target=self._execute, args=(job["id"], stop), daemon=True
        )
        self.workers[job["id"]] = (thread, stop)
        thread.start()
        return job

    def _fresh(self, observation: Json, maximum_age: float) -> None:
        age = time.time() - observation["sampled_at"]
        if (
            observation["quality"] != "valid"
            or observation.get("clock_mapping_valid", True) is False
            or observation["calibration"]["id"] != self.hardware.calibration_id
            or observation["source"] != self.hardware.operation_devices["observe"]
            or not 0 <= age <= maximum_age
        ):
            raise CodingError(
                "stale_observation",
                "Observation is stale, invalid or uses a different calibration",
            )

    def events(self, args: Json) -> Json:
        if self.closing:
            raise CodingError("host_stopping", "Host is shutting down")
        job_id = args["job_id"]
        after = int(number(args.get("after", 0), "after", 0, 1e15))
        until = args.get("until", "events")
        if until not in ("events", "terminal"):
            raise CodingError("invalid_arguments", "until must be events or terminal")
        wait = number(args.get("wait", 0), "wait", 0, 30 if until == "terminal" else 10)
        deadline = time.monotonic() + wait
        job = self._job(job_id)
        if job["disconnect_policy"] == "cancel" and job["status"] not in TERMINAL:
            # Server wakeups cannot renew a disconnected client's lease.
            job["client_deadline"] = time.time() + 5
            self._save(job)
            deadline = min(deadline, time.monotonic() + 4)
        while True:
            if self.closing:
                raise CodingError("host_stopping", "Host is shutting down")
            job = self._job(job_id)
            ready = (
                job["status"] in TERMINAL
                or time.monotonic() >= deadline
                or (
                    until == "terminal"
                    and (job["status"] == "cancelling" or job.get("uncertain_action"))
                )
            )
            # A terminal wait batches feedback, not device execution. Do not
            # repeatedly read the same page on every local action wakeup.
            if until == "events" or ready:
                value = self.store.events(job_id, after)
                if ready or value["events"]:
                    return {**value, "job": job}
            self.condition.wait(min(1, max(0, deadline - time.monotonic())))

    def cancel(self, job_id: str) -> Json:
        job = self._job(job_id)
        if job["status"] in TERMINAL:
            return job
        if job["status"] != "cancelling":
            job["status"] = "cancelling"
            self.store.event(job, "cancel_requested")
            # Closing admission is separate from stopping worker and driver.
            self._save(job)
        if job_id in self.workers:
            self.workers[job_id][1].set()
        return job

    def gateway(self, key: str, op: str, args: Json) -> Json:
        if op == "call":
            return self._device_call(key, args)
        with self.condition:
            job = self._job(key)
            if job["status"] != "running" or time.time() >= job["deadline"]:
                raise CodingError("job_stopped", "Job no longer has device authority")
            report = self._program_report(job, args) if op == "report" else None
            counts, limits = job["counts"], job["limits"]
            counts["events"] += 1
            if counts["events"] > limits["max_events"]:
                raise CodingError("resource_limit", "Event budget exhausted")
            if op in {"log", "report"}:
                data = report if report is not None else {"text": str(args["text"])}
                size = canonical(data) if report is not None else data["text"]
                counts["log_bytes"] += len(size.encode())
                if counts["log_bytes"] > limits["max_log_bytes"]:
                    self._save(job)
                    raise CodingError("resource_limit", "Log budget exhausted")
                with self.store.transaction():
                    event = self.store.event(
                        job, "program_report" if report is not None else "log", **data
                    )
                    if report is not None:
                        job["latest_report"] = event
                    self._save(job)
                    return event
            self._save(job)
            device = self.hardware.operation_devices.get(op)
            if op in {"observe", "move", "query"} and (
                device is None or op not in job["devices"].get(device, [])
            ):
                raise CodingError(
                    "device_grant",
                    f"No {device}.{op} grant" if device else f"No device supports {op}",
                )
            if op == "devices":
                return self.hardware.discover()
            if op == "observe":
                return self.hardware.observe(job)
            if op in {"move", "query"}:
                return self._action(job, op, args)
            raise CodingError("device_grant", "Worker operation is not permitted")

    def _program_report(self, job: Json, args: Json) -> Json:
        """Validate a small program claim, without judging it or operating devices."""
        if not isinstance(args, dict) or set(args) != {
            "stage",
            "status",
            "measurements",
            "observations",
        }:
            raise CodingError("invalid_report", "Report fields do not match the SDK")
        stage, status = args["stage"], args["status"]
        observations = args["observations"]
        if (
            not isinstance(stage, str)
            or not stage.strip()
            or len(stage) > 128
            or not isinstance(status, str)
            or status not in {"pass", "fail", "unknown"}
            or not isinstance(args["measurements"], dict)
            or not isinstance(observations, list)
            or len(observations) > 4
            or any(not isinstance(key, str) for key in observations)
        ):
            raise CodingError(
                "invalid_report",
                "Report needs a stage, pass/fail/unknown, measurements object and up to four observation IDs",
            )
        try:
            encoded = canonical(args).encode()
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise CodingError(
                "invalid_report",
                "Report must be finite JSON: NaN, Infinity and non-JSON values are invalid. Check empty or non-finite measurements before reporting.",
            ) from exc
        if len(encoded) > 2048:
            raise CodingError(
                "invalid_report",
                "Report exceeds 2048 UTF-8 bytes; keep selected measurements only",
            )
        evidence = []
        for key in observations:
            observation = self.store.get("observation", key)
            if (
                observation.get("job_id") != job["id"]
                or observation.get("program_version") != job["program_version"]
            ):
                raise CodingError(
                    "invalid_report", "Report evidence belongs to another job"
                )
            evidence.append({"id": key, "sampled_at": observation["sampled_at"]})
        report = {**args, "source": "program", "observations": evidence}
        if len(canonical(report).encode()) > 2048:
            raise CodingError(
                "invalid_report", "Report with evidence exceeds 2048 UTF-8 bytes"
            )
        return report

    def _device_call(self, key: str, args: Json) -> Json:
        """Authorize/persist before dispatch, without locking out cancellation.

        Operator-installed drivers implement invoke and reconcile. invoke must
        honor the supplied deadline/cancellation event and never silently retry.
        Unknown outcomes fence subsequent calls and remain unknown on recovery
        unless the driver can supply evidence for the original command ID.
        """
        with self.condition:
            job = self._job(key)
            if job["status"] != "running" or time.time() >= job["deadline"]:
                raise CodingError("job_stopped", "Job no longer has device authority")
            device, operation = args.get("device"), args.get("operation")
            if (
                not isinstance(device, str)
                or not isinstance(operation, str)
                or operation not in job["devices"].get(device, [])
                or not hasattr(self.hardware, "invoke")
            ):
                raise CodingError(
                    "device_grant", "Unsupported or ungranted driver operation"
                )
            command_id = args.get("command_id")
            if not isinstance(command_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,100}", command_id
            ):
                raise CodingError("invalid_command", "A bounded command_id is required")
            action_key = key + ":" + command_id
            fingerprint = digest(args)
            try:
                previous = self.store.get("action", action_key)
            except CodingError:
                previous = None
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise CodingError(
                        "command_conflict",
                        "Command ID reused with different parameters",
                    )
                if previous["status"] != "confirmed":
                    raise CodingError(
                        "reconciliation_required",
                        "Original call outcome is not confirmed",
                    )
                if "error" in previous:
                    raise CodingError("device_error", previous["error"])
                return {"value": previous["result"]}
            if job.get("uncertain_action"):
                raise CodingError(
                    "reconciliation_required",
                    "Query the original command before further calls",
                )
            observation = self.store.get("observation", args["based_on"])
            if observation.get("job_id") != key:
                raise CodingError(
                    "observation_conflict", "Call requires this job's observation"
                )
            self._fresh(observation, job["limits"]["observation_age_s"])
            job["counts"]["actions"] += 1
            job["counts"]["events"] += 1
            if job["counts"]["events"] > job["limits"]["max_events"]:
                raise CodingError("resource_limit", "Event budget exhausted")
            if job["counts"]["actions"] > job["limits"]["max_actions"]:
                raise CodingError("resource_limit", "Action budget exhausted")
            action = {
                "id": action_key,
                "job_id": key,
                "program_version": job["program_version"],
                "command_id": command_id,
                "fingerprint": fingerprint,
                "status": "dispatching",
                "device_operation": operation,
                "device": device,
                "args": args,
                "timestamp": time.time(),
            }
            with self.store.transaction():
                self._save(job)
                self.store.put("action", action_key, action)
                self._device_event(job, "action_intent", action)
            stop = self.workers[key][1]
        try:
            result = self.hardware.invoke(
                action, deadline=job["deadline"], cancelled=stop
            )
        except Exception as exc:
            with self.condition:
                job = self._job(key)
                # A driver-confirmed API exception differs from a lost reply.
                confirmed_error = (
                    isinstance(exc, CodingError) and exc.code == "device_error"
                )
                action.update(
                    status="confirmed" if confirmed_error else "unknown",
                    error=f"{type(exc).__name__}: {exc}",
                )
                if not confirmed_error:
                    job["uncertain_action"] = action_key
                with self.store.transaction():
                    self._save(job)
                    self.store.put("action", action_key, action)
                    self._device_event(job, "action_result", action)
            if confirmed_error:
                raise
            raise CodingError(
                "action_unknown", "Query original command; never replay: " + str(exc)
            ) from exc
        with self.condition:
            action.update(status="confirmed", result=result)
            with self.store.transaction():
                self.store.put("action", action_key, action)
                self._device_event(self._job(key), "action_result", action)
        return {"value": result}

    def _device_event(self, job: Json, kind: str, action: Json) -> None:
        # Full numerical payloads stay durable in the action store, not in
        # every event page/model context. Images use the existing media store.
        summary = {k: v for k, v in action.items() if k not in {"args", "result"}}
        self.store.event(job, kind, action=summary)

    def _action(self, job: Json, op: str, args: Json) -> Json:
        command_id = args["command_id"]
        if not isinstance(command_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,100}", command_id
        ):
            raise CodingError(
                "invalid_command", "A bounded stable command_id is required"
            )
        key = job["id"] + ":" + command_id
        try:
            old = self.store.get("action", key)
        except CodingError:
            old = None
        if op == "query":
            if old is None:
                return {"status": "not_dispatched", "command_id": command_id}
            if old["status"] in {"unknown", "dispatching"}:
                if "device_operation" in old:
                    result = self.hardware.reconcile(old)
                    if result.get("status") == "confirmed":
                        old.update(
                            status="confirmed",
                            result=result["result"],
                            reconciled_at=time.time(),
                        )
                        self.store.put("action", key, old)
                        job.pop("uncertain_action", None)
                        self._save(job)
                    self._device_event(job, "action_reconciled", old)
                    return old
                evidence = self._observe(job)
                position = [evidence["state"]["x_mm"], evidence["state"]["y_mm"]]
                if position == old["target_mm"]:
                    old.update(
                        status="confirmed",
                        reconciled_at=time.time(),
                        observation=evidence["id"],
                    )
                    self.store.put("action", key, old)
                    job.pop("uncertain_action", None)
                    self._save(job)
                self.store.event(
                    job, "action_reconciled", action=old, observation=evidence["id"]
                )
            return old
        fingerprint = digest({k: v for k, v in args.items() if k != "lose_ack"})
        if old:
            if old["fingerprint"] != fingerprint:
                raise CodingError(
                    "command_conflict", "Command ID reused with different parameters"
                )
            return old
        if job.get("uncertain_action"):
            raise CodingError(
                "reconciliation_required",
                "Query uncertain command and inspect observations before new motion",
            )
        limits = job["limits"]
        dx = number(args["dx"], "dx", -limits["max_step_mm"], limits["max_step_mm"])
        dy = number(args["dy"], "dy", -limits["max_step_mm"], limits["max_step_mm"])
        speed = number(args["speed"], "speed", 0.01, limits["max_speed_mm_s"])
        observation = self.store.get("observation", args["based_on"])
        if observation.get("job_id") != job["id"] or observation[
            "device_revision"
        ] != self.hardware.state.get("revision", 0):
            raise CodingError(
                "observation_conflict",
                "Motion needs a current device revision observed by this job",
            )
        self._fresh(observation, limits["observation_age_s"])
        job["counts"]["actions"] += 1
        if job["counts"]["actions"] > limits["max_actions"]:
            raise CodingError("resource_limit", "Action budget exhausted")
        self._save(job)
        action: Json = {
            "id": key,
            "command_id": command_id,
            "job_id": job["id"],
            "program_version": job["program_version"],
            "fingerprint": fingerprint,
            "status": "dispatching",
            "based_on": observation["id"],
            "args": args,
            "timestamp": time.time(),
            "target_mm": [self.hardware.state["x"] + dx, self.hardware.state["y"] + dy],
        }
        self.store.put("action", key, action)
        self.store.event(job, "action_intent", action=action)
        try:
            result = self.hardware.move(dx, dy, speed)
        except CodingError as exc:
            action.update(status="rejected", error=exc.code)
            self.store.put("action", key, action)
            self.store.event(job, "action_rejected", action=action)
            raise
        action.update(
            status="unknown" if args.get("lose_ack") else "confirmed", result=result
        )
        if action["status"] == "unknown":
            action.pop("result")
            job["uncertain_action"] = key
            self._save(job)
        self.store.put("action", key, action)
        self.store.event(job, "action_result", action=action)
        return action

    def _execute(self, key: str, stop: threading.Event) -> None:
        process: subprocess.Popen[bytes] | None = None
        status = "failed"
        error: str | None = None
        stderr = bytearray()
        live_read: int | None = None
        live_write: int | None = None
        try:
            with self.condition:
                job = self._job(key)
                if stop.is_set():
                    status = "cancelled"
                    return
                program = self.store.get("program", job["program_version"])
                live_read, live_write = os.pipe()
                command = self.programs.command(
                    program,
                    {
                        "job_id": key,
                        "program_version": program["id"],
                        "parameters": job["parameters"],
                        "limits": job["limits"],
                        "liveness_fd": live_read,
                    },
                )
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.store.root,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "LANG": "en_US.UTF-8",
                        "OPENBLAS_NUM_THREADS": "1",
                        "OMP_NUM_THREADS": "1",
                        "MKL_NUM_THREADS": "1",
                        "NUMEXPR_NUM_THREADS": "1",
                    },
                    start_new_session=True,
                    pass_fds=(live_read,),
                )
                os.close(live_read)
                live_read = None
                job.update(status="running", started_at=time.time(), pid=process.pid)
                self.store.event(job, "started", pid=process.pid)
                self._save(job)
            assert process.stdout and process.stdin and process.stderr
            pending = bytearray()
            outgoing = bytearray()
            os.set_blocking(process.stdin.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "rpc")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                next_refresh = 0.0
                while process.poll() is None or selector.get_map():
                    with self.condition:
                        # Device payloads may span hundreds of pipe reads. Poll
                        # mutable host state at a control cadence, not per 8 KiB
                        # of data (especially on network-backed durable stores).
                        if time.monotonic() >= next_refresh:
                            job = self._job(key)
                            next_refresh = time.monotonic() + 0.05
                        now = time.time()
                        if (
                            job["disconnect_policy"] == "cancel"
                            and now >= job["client_deadline"]
                        ):
                            job = self._job(key)
                            if now >= job["client_deadline"]:
                                self.cancel(key)
                        if stop.is_set() or now >= job["deadline"]:
                            status = "cancelled" if stop.is_set() else "timed_out"
                            break
                        if (
                            resident_bytes(process.pid)
                            > job["limits"]["memory_mb"] * 1024 * 1024
                        ):
                            raise CodingError(
                                "resource_limit", "Resident memory budget exceeded"
                            )
                    for selected, _ in selector.select(0.02):
                        if selected.data == "reply":
                            try:
                                written = os.write(selected.fd, outgoing)
                                del outgoing[:written]
                            except BlockingIOError:
                                continue
                            if not outgoing:
                                selector.unregister(process.stdin)
                            continue
                        chunk = os.read(selected.fd, 65536)
                        if not chunk:
                            selector.unregister(selected.fileobj)
                            continue
                        if selected.data == "stderr":
                            stderr.extend(chunk)
                            if len(stderr) > job["limits"]["max_log_bytes"]:
                                raise CodingError(
                                    "resource_limit", "stderr budget exhausted"
                                )
                            continue
                        pending.extend(chunk)
                        if len(pending) > MAX_WIRE:
                            raise CodingError(
                                "resource_limit", "Worker request exceeds wire budget"
                            )
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            request = json.loads(line)
                            if request["op"] != "call" and len(line) > 64_000:
                                raise CodingError(
                                    "resource_limit",
                                    "Worker request exceeds 64,000 bytes",
                                )
                            try:
                                reply: Json = {
                                    "result": self.gateway(
                                        key, request["op"], request.get("args", {})
                                    )
                                }
                            except CodingError as exc:
                                reply = {"error": exc.code, "message": str(exc)}
                                if exc.code == "resource_limit":
                                    raise
                            if not outgoing:
                                selector.register(
                                    process.stdin, selectors.EVENT_WRITE, "reply"
                                )
                            outgoing.extend((json.dumps(reply) + "\n").encode())
                            if len(outgoing) > MAX_WIRE:
                                raise CodingError(
                                    "resource_limit",
                                    "Worker response backlog exceeds wire budget",
                                )
                    if not selector.get_map() and process.poll() is None:
                        stop.wait(0.02)
                else:
                    status = "exited" if process.returncode == 0 else "failed"
        except Exception as exc:  # noqa: BLE001 -- supervisor must stop devices on every failure
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if live_read is not None:
                os.close(live_read)
            if live_write is not None:
                os.close(live_write)
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream:
                        stream.close()
            with self.condition:
                job = self._job(key)
                confirmation = self.hardware.stop()
                job.update(
                    status=status,
                    exit_code=process.returncode if process else None,
                    finished_at=time.time(),
                    stop_confirmation=confirmation,
                )
                if error:
                    self.store.event(job, "exception", message=error)
                    job["error"] = error
                if stderr:
                    self.store.event(
                        job, "stderr", text=stderr.decode(errors="replace")[:65536]
                    )
                self.store.event(job, "stop_confirmed", confirmation=confirmation)
                try:
                    self._reconcile(job)
                    job["physical_verification"] = self._verify(job)
                except Exception as exc:  # noqa: BLE001 -- supervisor must stop devices on every failure
                    job["physical_verification"] = {
                        "status": "unknown",
                        "reason": str(exc),
                    }
                self.store.event(
                    job,
                    "finished",
                    status=status,
                    exit_code=job["exit_code"],
                    physical_verification=job["physical_verification"],
                )
                self._save(job)

    def _reconcile(self, job: Json) -> None:
        for action in self.store.all("action"):
            if action["job_id"] == job["id"] and action["status"] in {
                "unknown",
                "dispatching",
            }:
                self._action(job, "query", {"command_id": action["command_id"]})

    def _verify(self, job: Json) -> Json:
        samples = []
        for _ in range(job["goal"]["samples"]):
            samples.append(self._observe(job))
            time.sleep(job["goal"]["sample_interval_s"])
        value: Json = {
            **self.hardware.evaluate(samples, job),
            "job_id": job["id"],
            "program_version": job["program_version"],
            "method": job["goal"]["method"],
            "goal": job["goal"],
            "simulated": self.hardware.simulated,
            "observations": [o["id"] for o in samples],
            "window": [samples[0]["sampled_at"], samples[-1]["sampled_at"]],
        }
        self.store.put("verification", job["id"], value)
        return value

    def close(self) -> None:
        with self.condition:
            self.closing = True
            self.condition.notify_all()
            for key in self.workers:
                self.cancel(key)
        for thread, _ in self.workers.values():
            thread.join(timeout=10)
        self.hardware.close()
        # Stop devices first, then let an in-flight publication finish its writes.
        with self.publication_lock:
            self.store.db.close()
