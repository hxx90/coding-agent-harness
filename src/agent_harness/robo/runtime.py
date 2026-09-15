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
import threading
import time
import uuid
from pathlib import Path

from agent_harness.coding.media import MediaStore
from agent_harness.coding.types import CodingError, Json

from .hardware import MHS_STATUS, SimHardware
from .programs import Programs
from .store import Store, digest

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
}


def resident_bytes(pid: int) -> int:
    """macOS PROC_PIDTASKINFO, sampled by the host (not a memory reservation)."""
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
    def __init__(self, root: Path, *, backend: str = "sim") -> None:
        if backend != "sim":
            raise CodingError(
                "mhs_unavailable", MHS_STATUS["reason"], details=MHS_STATUS
            )
        self.store = Store(root)
        self.media = MediaStore(root / "media")
        self.hardware = SimHardware(self.store, self.media)
        self.programs = Programs(self.store)
        self.condition = threading.Condition(threading.RLock())
        self.workers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.closing = False
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
                self.store.event(job, "host_recovered", observation=self._observe(job))
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
        with self.condition:
            if op == "health":
                return {
                    "status": "ready",
                    "backend": "sim",
                    "simulated": True,
                    "protocol": 1,
                    "mhs": MHS_STATUS,
                }
            if op == "devices":
                return self.hardware.discover()
            if op == "snapshot":
                keys = args.get("job_ids", [])
                if not isinstance(keys, list) or len(keys) > 8:
                    raise CodingError(
                        "invalid_arguments", "snapshot accepts up to eight jobs"
                    )
                return {
                    "sampled_at": time.time(),
                    "source": "sim-state",
                    "simulated": True,
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
            if op == "publish":
                if self.closing:
                    raise CodingError("host_stopping", "Host is shutting down")
                return self.programs.publish(args)
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
                "resource_busy", "sim-workcell is owned or stop remains unconfirmed"
            )
        program = self.store.get("program", args["program_version"])
        manifest = program["manifest"]
        grants = manifest.get("devices")
        allowed = {"sim-camera": ["observe"], "sim-stage": ["move", "query"]}
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
        limits = dict(DEFAULT_LIMITS)
        for key, value in manifest.get("limits", {}).items():
            if key not in limits:
                raise CodingError("invalid_limit", f"Unsupported limit: {key}")
            number(value, key, 0.01, DEFAULT_LIMITS[key])
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
        definitions = manifest.get("parameters", {})
        if set(parameters) != set(definitions):
            raise CodingError(
                "invalid_parameters", "Parameters must exactly match the manifest"
            )
        for key, spec in definitions.items():
            low = number(spec["min"], key + ".min", -1e6, 1e6)
            high = number(spec["max"], key + ".max", low, 1e6)
            number(parameters[key], key, low, high)
        policy = args.get("disconnect_policy", "continue_bounded")
        if policy not in {"continue_bounded", "cancel"}:
            raise CodingError("invalid_policy", "Use continue_bounded or cancel")
        observation = self.store.get("observation", args["based_on"])
        self._fresh(observation, limits["observation_age_s"])
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
            "resource": "sim-workcell",
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
            "goal": {
                "method": "sim-encoder-window-v1",
                "target_mm": [0, 0],
                "tolerance_mm": 1,
                "samples": 3,
                "sample_interval_s": 0.02,
            },
            "simulated": True,
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

    @staticmethod
    def _fresh(observation: Json, maximum_age: float) -> None:
        age = time.time() - observation["sampled_at"]
        if (
            observation["quality"] != "valid"
            or observation["calibration"]["id"] != "sim-cal-v1"
            or not 0 <= age <= maximum_age
        ):
            raise CodingError(
                "stale_observation",
                "Observation is stale, invalid or uses a different calibration",
            )

    def events(self, args: Json) -> Json:
        job_id = args["job_id"]
        after = int(number(args.get("after", 0), "after", 0, 1e15))
        wait = number(args.get("wait", 0), "wait", 0, 10)
        deadline = time.monotonic() + wait
        job = self._job(job_id)
        if job["disconnect_policy"] == "cancel" and job["status"] not in TERMINAL:
            # Server wakeups cannot renew a disconnected client's lease.
            job["client_deadline"] = time.time() + 5
            self._save(job)
            deadline = min(deadline, time.monotonic() + 4)
        while True:
            job = self._job(job_id)
            value = self.store.events(job_id, after)
            if (
                value["events"]
                or job["status"] in TERMINAL
                or time.monotonic() >= deadline
            ):
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
        with self.condition:
            job = self._job(key)
            if job["status"] != "running" or time.time() >= job["deadline"]:
                raise CodingError("job_stopped", "Job no longer has device authority")
            counts, limits = job["counts"], job["limits"]
            counts["events"] += 1
            if counts["events"] > limits["max_events"]:
                raise CodingError("resource_limit", "Event budget exhausted")
            self._save(job)
            device = {
                "observe": "sim-camera",
                "move": "sim-stage",
                "query": "sim-stage",
            }.get(op)
            if device and op not in job["devices"].get(device, []):
                raise CodingError("device_grant", f"No {device}.{op} grant")
            if op == "devices":
                return self.hardware.discover()
            if op == "observe":
                return self.hardware.observe(job)
            if op == "log":
                text = str(args["text"])
                counts["log_bytes"] += len(text.encode())
                self._save(job)
                if counts["log_bytes"] > limits["max_log_bytes"]:
                    raise CodingError("resource_limit", "Log budget exhausted")
                return self.store.event(job, "log", text=text)
            if op in {"move", "query"}:
                return self._action(job, op, args)
            raise CodingError("device_grant", "Worker operation is not permitted")

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
                    env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
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
                while process.poll() is None or selector.get_map():
                    with self.condition:
                        job = self._job(key)
                        now = time.time()
                        if (
                            job["disconnect_policy"] == "cancel"
                            and now >= job["client_deadline"]
                        ):
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
                        chunk = os.read(selected.fd, 8192)
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
                        if len(pending) > 64_000:
                            raise CodingError(
                                "resource_limit", "Worker request exceeds 64,000 bytes"
                            )
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            request = json.loads(line)
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
                            if len(outgoing) > 2 * 1024 * 1024:
                                raise CodingError(
                                    "resource_limit",
                                    "Worker response backlog exceeds 2 MiB",
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
        valid = all(o["quality"] == "valid" for o in samples)
        distances = [
            math.hypot(o["state"]["x_mm"], o["state"]["y_mm"]) for o in samples
        ]
        status = (
            "unknown"
            if not valid
            else "succeeded"
            if max(distances) <= job["goal"]["tolerance_mm"]
            else "failed"
        )
        value: Json = {
            "status": status,
            "job_id": job["id"],
            "program_version": job["program_version"],
            "method": job["goal"]["method"],
            "goal": job["goal"],
            "simulated": True,
            "observations": [o["id"] for o in samples],
            "max_error_mm": max(distances),
            "window": [samples[0]["sampled_at"], samples[-1]["sampled_at"]],
        }
        self.store.put("verification", job["id"], value)
        return value

    def close(self) -> None:
        with self.condition:
            self.closing = True
            for key in self.workers:
                self.cancel(key)
        for thread, _ in self.workers.values():
            thread.join(timeout=10)
        self.store.db.close()
