"""Native macOS AVFoundation camera, shared by agent observations and workers.

This is a real camera adapter, not an MHS implementation. The capture process
keeps only its latest frame in memory; observation requests persist selected
frames. No audio is captured and no camera media is uploaded by this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import ClassVar

from filelock import FileLock, Timeout

from agent_harness.coding.media import MediaStore
from agent_harness.coding.types import CodingError, Json

from .hardware import MHS_STATUS, png
from .store import Store


def native_helper(root: Path) -> Path:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        raise CodingError(
            "camera_unavailable",
            "Native camera requires macOS and Xcode Command Line Tools (swiftc)",
        )
    source = Path(__file__).with_name("native_camera.swift")
    key = hashlib.sha256(source.read_bytes()).hexdigest()
    folder = root / "native" / key
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = folder / "robo-camera"
    try:
        with FileLock(str(folder / "build.lock"), timeout=45):
            if not binary.exists():
                temporary = folder / "robo-camera.tmp"
                result = subprocess.run(
                    ["swiftc", str(source), "-o", str(temporary)],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
                if result.returncode:
                    raise CodingError("camera_build", result.stderr[-4000:])
                temporary.chmod(0o700)
                temporary.replace(binary)
    except (Timeout, subprocess.TimeoutExpired, OSError) as exc:
        raise CodingError("camera_build", str(exc)) from exc
    return binary


def cameras(root: Path) -> Json:
    binary = native_helper(root)
    try:
        result = subprocess.run(
            [str(binary), "list"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise CodingError("camera_discovery", str(exc)) from exc
    if result.returncode:
        raise CodingError(
            "camera_discovery", result.stderr[-2000:] or result.stdout[-2000:]
        )
    return {
        "backend": "camera",
        "driver": "robo-avfoundation/0.1",
        "mhs_integrated": False,
        **json.loads(result.stdout),
    }


class CameraStream:
    """Bounded latest-frame buffer with native timestamps and EOF supervision."""

    def __init__(self, binary: Path, uid: str) -> None:
        self.condition = threading.Condition()
        self.frame: Json | None = None
        self.error: str | None = None
        self.stderr = bytearray()
        self.process = subprocess.Popen(
            [str(binary), "stream", uid],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.readers = [
            threading.Thread(target=self._read, daemon=True),
            threading.Thread(target=self._stderr, daemon=True),
        ]
        for thread in self.readers:
            thread.start()
        try:
            # Native authorization may wait 30s; leave time for the first frame.
            self.latest(after=0, timeout=35)
        except BaseException:
            self.close()
            raise

    def _read(self) -> None:
        assert self.process.stdout
        try:
            while True:
                line = self.process.stdout.readline(2 * 1024 * 1024)
                if not line:
                    raise ValueError("Camera stream ended or device disconnected")
                if not line.endswith(b"\n"):
                    raise ValueError("Camera frame exceeds the driver message limit")
                value = json.loads(line)
                if "error" in value:
                    raise ValueError(value["error"] + ": " + value["message"])
                width, height = value["width"], value["height"]
                if (
                    not isinstance(width, int)
                    or not isinstance(height, int)
                    or not (0 < width <= 640 and 0 < height <= 640)
                ):
                    raise ValueError("Unsupported camera dimensions")
                rgb = base64.b64decode(value["rgb"], validate=True)
                if len(rgb) != width * height * 3 or not math.isfinite(
                    value["sampled_at"]
                ):
                    raise ValueError("Invalid camera frame")
                with self.condition:
                    self.frame = {
                        **value,
                        "received_at": time.time(),
                        "received_monotonic": time.monotonic(),
                        "data": rgb,
                    }
                    self.condition.notify_all()
        except (ValueError, TypeError, KeyError, OSError) as exc:
            with self.condition:
                self.error = str(exc)
                self.condition.notify_all()

    def _stderr(self) -> None:
        assert self.process.stderr
        while data := self.process.stderr.read(1024):
            with self.condition:
                self.stderr.extend(data)
                self.stderr[:] = self.stderr[-4000:]

    def latest(self, *, after: int, timeout: float = 0.6) -> Json:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                if self.error or self.process.poll() is not None:
                    raise CodingError(
                        "camera_unavailable",
                        self.error
                        or self.stderr.decode(errors="replace")
                        or "Camera process exited",
                    )
                if (
                    self.frame
                    and self.frame["seq"] > after
                    and time.monotonic() - self.frame["received_monotonic"] <= 1
                    and 0 <= time.time() - self.frame["sampled_at"] <= 1
                ):
                    return dict(self.frame)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodingError(
                        "camera_timeout",
                        "No fresh camera frame. Check camera access in macOS Privacy & Security and whether the camera is available; restart the host after granting access.",
                    )
                self.condition.wait(remaining)

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        for thread in self.readers:
            thread.join(timeout=1)
        for stream in (self.process.stdout, self.process.stderr):
            if stream:
                stream.close()


class CameraHardware:
    backend = "camera"
    simulated = False
    operation_devices: ClassVar[dict[str, str]] = {"observe": "native-camera"}
    grants: ClassVar[Json] = {"native-camera": ["observe"]}
    goal: ClassVar[Json] = {
        "method": "camera-acquisition-window-v1",
        "samples": 3,
        "sample_interval_s": 0.02,
    }

    def __init__(
        self, store: Store, media: MediaStore, selector: str | None = None
    ) -> None:
        self.store, self.media = store, media
        found = cameras(store.root)
        choices = (
            [
                d
                for d in found["devices"]
                if d["uid"] == selector or d["name"] == selector
            ]
            if selector
            else [d for d in found["devices"] if d["built_in"]]
        )
        if len(choices) != 1:
            raise CodingError(
                "camera_selection",
                "Select exactly one camera by UID or name using --camera; list devices with robo cameras",
            )
        self.device = choices[0]
        self.resource = (
            "camera:" + hashlib.sha256(self.device["uid"].encode()).hexdigest()[:16]
        )
        self.calibration_id = "uncalibrated:" + self.resource
        try:
            self.state = store.get("device", self.resource)
        except CodingError:
            self.state = {"protected": False, "revision": 0, "seq": 0}
        binary = native_helper(store.root)
        self.driver_hash = binary.parent.name
        self.capture_session = uuid.uuid4().hex
        self.stream = (
            None
            if self.state["protected"]
            else CameraStream(binary, self.device["uid"])
        )
        self.last_sequence = 0

    def discover(self) -> Json:
        return {
            "backend": self.backend,
            "simulated": False,
            "mhs": MHS_STATUS,
            "resource": self.resource,
            "protection_latched": self.state["protected"],
            "capture_process_running": self.stream is not None
            and self.stream.process.poll() is None,
            "devices": [
                {
                    "id": "native-camera",
                    **self.device,
                    "simulated": False,
                    "driver": "robo-avfoundation/0.1",
                    "driver_source_sha256": self.driver_hash,
                    "operations": ["observe"],
                    "size": [self.stream.frame["width"], self.stream.frame["height"]]
                    if self.stream and self.stream.frame
                    else None,
                    "delivery_fps_max": 5.6,
                    "pixel_origin": "top-left",
                    "mirrored": False,
                    "frame": "camera_px",
                    "calibration": "uncalibrated",
                    "audio": False,
                }
            ],
        }

    def observe(self, job: Json | None = None) -> Json:
        if self.state["protected"] or self.stream is None:
            raise CodingError("device_protected", "Camera acquisition has been stopped")
        frame = self.stream.latest(after=self.last_sequence)
        self.last_sequence = frame["seq"]
        self.state["seq"] += 1
        rgb = frame["data"]
        observation: Json = {
            "id": "obs_" + uuid.uuid4().hex,
            "source": "native-camera",
            "device_uid": self.device["uid"],
            "capture_session": self.capture_session,
            "driver": "robo-avfoundation/0.1",
            "driver_source_sha256": self.driver_hash,
            "simulated": False,
            "backend": self.backend,
            "sampled_at": frame["sampled_at"],
            "received_at": frame["received_at"],
            "driver_received_at": frame.get("driver_received_at"),
            "source_pts_s": frame["source_pts_s"],
            "timestamp_kind": frame["timestamp_kind"],
            "clock_mapping_valid": frame["clock_mapping_valid"],
            "clock": "avfoundation-host-clock"
            if frame["clock_mapping_valid"]
            else "host-wall-receive-estimate",
            "sync_error_s": None,
            "seq": self.state["seq"],
            "capture_seq": frame["seq"],
            "frame": "camera_px",
            "unit": "pixel",
            "pixel_origin": "top-left",
            "calibration": {
                "id": self.calibration_id,
                "status": "uncalibrated",
                "intrinsics": None,
                "extrinsics": None,
            },
            "quality": "valid",
            "device_revision": 0,
            "state": {
                "width": frame["width"],
                "height": frame["height"],
                "source_width": frame["source_width"],
                "source_height": frame["source_height"],
                "mean_rgb": [
                    round(sum(rgb[i::3]) / (len(rgb) // 3), 3) for i in range(3)
                ],
                "pixel_min": min(rgb),
                "pixel_max": max(rgb),
                "protected": False,
            },
        }
        self.state["last_sampled_at"] = frame["sampled_at"]
        self.store.put("device", self.resource, self.state)
        if job:
            observation.update(job_id=job["id"], program_version=job["program_version"])
        observation["image"] = self.media.put(
            png(frame["width"], frame["height"], rgb),
            "image/png",
            metadata={k: v for k, v in observation.items() if k != "state"},
        )
        self.store.put("observation", observation["id"], observation, immutable=True)
        self.store.event(job, "observation", observation=observation)
        return {
            **observation,
            "pixels": {
                "width": frame["width"],
                "height": frame["height"],
                "rgb": frame["rgb"],
            },
        }

    def evaluate(self, samples: list[Json], job: Json) -> Json:
        known_time = all(o["clock_mapping_valid"] for o in samples)
        success = len({o["capture_seq"] for o in samples}) == len(samples) and all(
            o["quality"] == "valid" for o in samples
        )
        return {
            "status": "unknown",
            "reason": "No physical scene predicate is configured for this camera-only job",
            "capture_verification": {
                "status": "unknown"
                if not known_time
                else "succeeded"
                if success
                else "failed",
                "frames": len(samples),
                "method": "fresh-distinct-camera-frames",
                "capture_time_known": known_time,
            },
        }

    def stop(self, *, protect: bool = False) -> Json:
        if protect:
            self.state["protected"] = True
            self.store.put("device", self.resource, self.state)
            self.close()
        return {
            "status": "confirmed",
            "confirmed_at": time.time(),
            "simulated": False,
            "operation": "observation-access-revoked",
            "capture_process_running": self.stream is not None
            and self.stream.process.poll() is None,
            "protection_latched": self.state["protected"],
        }

    def move(self, dx: float, dy: float, speed: float) -> Json:
        raise CodingError("device_grant", "Native camera has no motion capability")

    def close(self) -> None:
        if self.stream:
            self.stream.close()
