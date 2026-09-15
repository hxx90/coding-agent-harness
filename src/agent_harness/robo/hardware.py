"""Simulation hardware and explicit MHS availability.

These operations are internal Robo semantics. No MHS SDK is publicly available
from the official preview sources checked on 2026-09-15.
"""

from __future__ import annotations

import base64
import math
import struct
import time
import uuid
import zlib
from typing import ClassVar, Protocol

from agent_harness.coding.media import MediaStore
from agent_harness.coding.types import CodingError, Json

from .store import Store

MHS_STATUS: Json = {
    "backend": "mhs",
    "available": False,
    "sdk_version": None,
    "spec_version": None,
    "checked": "2026-09-15",
    "reason": "Application-only research preview; no public SDK/spec release linked by official sources. Hardware and preview access are required.",
    "sources": [
        "https://www.modelhardwarestandard.com/",
        "https://www.anthropic.com/news/model-hardware-standard-research-preview",
    ],
}


def png(width: int, height: int, rgb: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data))
        )

    rows = b"".join(
        b"\x00" + rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class Hardware(Protocol):
    backend: str
    simulated: bool
    resource: str
    calibration_id: str
    operation_devices: ClassVar[dict[str, str]]
    grants: ClassVar[Json]
    goal: ClassVar[Json]
    state: Json

    def discover(self) -> Json: ...
    def observe(self, job: Json | None = None) -> Json: ...
    def evaluate(self, samples: list[Json], job: Json) -> Json: ...
    def move(self, dx: float, dy: float, speed: float) -> Json: ...
    def stop(self, *, protect: bool = False) -> Json: ...
    def close(self) -> None: ...


class SimHardware:
    """One simulated workcell; position is persistent, motion is instantaneous.

    A shared workcell resource also protects the camera/target/stage relationship.
    Real motion, stop latency, clock synchronization and calibration are untested.
    """

    backend = "sim"
    simulated = True
    resource = "sim-workcell"
    calibration_id = "sim-cal-v1"
    operation_devices: ClassVar[dict[str, str]] = {
        "observe": "sim-camera",
        "move": "sim-stage",
        "query": "sim-stage",
    }
    grants: ClassVar[Json] = {"sim-camera": ["observe"], "sim-stage": ["move", "query"]}
    goal: ClassVar[Json] = {
        "method": "sim-encoder-window-v1",
        "target_mm": [0, 0],
        "tolerance_mm": 1,
        "samples": 3,
        "sample_interval_s": 0.02,
    }

    def __init__(self, store: Store, media: MediaStore) -> None:
        self.store, self.media = store, media
        try:
            self.state = store.get("device", "sim-stage")
        except CodingError:
            self.state = {"x": 16.0, "y": -12.0, "seq": 0, "protected": False}
            self._save()

    def _save(self) -> None:
        self.store.put("device", "sim-stage", self.state)

    def discover(self) -> Json:
        return {
            "backend": "sim",
            "simulated": True,
            "mhs": MHS_STATUS,
            "devices": [
                {
                    "id": "sim-camera",
                    "driver": "robo-sim/0.1",
                    "operations": ["observe"],
                    "frame": "camera_px",
                    "calibration": "sim-cal-v1",
                    "size": [64, 64],
                },
                {
                    "id": "sim-stage",
                    "driver": "robo-sim/0.1",
                    "operations": ["move", "query", "stop"],
                    "unit": "mm",
                    "frame": "stage_xy",
                    "travel": [-24, 24],
                    "max_step_mm": 4,
                    "max_speed_mm_s": 10,
                    "completion": "instantaneous simulated displacement",
                },
            ],
            "resource": "sim-workcell",
            "protection_latched": self.state["protected"],
        }

    def observe(self, job: Json | None = None) -> Json:
        self.state["seq"] += 1
        self._save()
        now = time.time()
        width = height = 64
        cx, cy = round(32 + self.state["x"]), round(32 + self.state["y"])
        pixels = bytearray([20, 20, 20] * width * height)
        for y in range(height):
            for x in range(width):
                offset = (y * width + x) * 3
                if x == 32 or y == 32:
                    pixels[offset : offset + 3] = bytes([60, 60, 60])
                if abs(x - cx) <= 2 and abs(y - cy) <= 2:
                    pixels[offset : offset + 3] = bytes([240, 30, 30])
        observation: Json = {
            "id": "obs_" + uuid.uuid4().hex,
            "source": "sim-camera",
            "simulated": True,
            "sampled_at": now,
            "received_at": time.time(),
            "seq": self.state["seq"],
            "frame": "camera_px",
            "unit": "pixel",
            "calibration": {
                "id": "sim-cal-v1",
                "pixels_per_mm": 1,
                "stage_to_image": [[1, 0], [0, 1]],
            },
            "quality": "valid",
            "device_revision": self.state.get("revision", 0),
            "clock": "host-wall",
            "sync_error_s": 0,
            "state": {
                "x_mm": self.state["x"],
                "y_mm": self.state["y"],
                "moving": False,
                "protected": self.state["protected"],
            },
        }
        if job:
            observation.update(job_id=job["id"], program_version=job["program_version"])
        observation["image"] = self.media.put(
            png(width, height, pixels),
            "image/png",
            metadata={k: v for k, v in observation.items() if k != "state"},
        )
        self.store.put("observation", observation["id"], observation, immutable=True)
        self.store.event(job, "observation", observation=observation)
        # Raw RGB is a local sensor sample; local programs perform the perception.
        return {
            **observation,
            "pixels": {
                "width": width,
                "height": height,
                "rgb": base64.b64encode(pixels).decode(),
            },
        }

    def move(self, dx: float, dy: float, speed: float) -> Json:
        if self.state["protected"]:
            raise CodingError("device_protected", "Simulation protection is latched")
        if (
            not all(math.isfinite(n) for n in (dx, dy, speed))
            or max(abs(dx), abs(dy)) > 4
            or not 0 < speed <= 10
        ):
            raise CodingError("device_limit", "Motion exceeds simulated driver limits")
        x, y = self.state["x"] + dx, self.state["y"] + dy
        if max(abs(x), abs(y)) > 24:
            raise CodingError("device_limit", "Stage travel exceeds +/-24 mm")
        self.state.update(x=x, y=y, revision=self.state.get("revision", 0) + 1)
        self._save()
        return {"status": "confirmed", "position_mm": [x, y], "simulated": True}

    def stop(self, *, protect: bool = False) -> Json:
        if protect:
            self.state["protected"] = True
        self._save()
        return {
            "status": "confirmed",
            "moving": False,
            "simulated": True,
            "protection_latched": self.state["protected"],
            "confirmed_at": time.time(),
        }

    def evaluate(self, samples: list[Json], job: Json) -> Json:
        distances = [
            math.hypot(o["state"]["x_mm"], o["state"]["y_mm"]) for o in samples
        ]
        valid = all(o["quality"] == "valid" for o in samples)
        status = (
            "unknown"
            if not valid
            else "succeeded"
            if max(distances) <= job["goal"]["tolerance_mm"]
            else "failed"
        )
        return {"status": status, "max_error_mm": max(distances)}

    def close(self) -> None:
        pass
