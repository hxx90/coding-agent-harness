"""Frozen, stdlib-only program SDK/worker. Communicates over inherited pipes.

The host owns authorization, motion constraints, timeouts and verification.
This file is copied into ProgramVersion's runtime; no live repo imports occur.
"""

from __future__ import annotations

import sys

# Establish the frozen dependency path before importing non-builtin modules.
_runtime = sys.argv[0].rsplit("/", 1)[0]
if __name__ == "__main__":
    sys.path[:] = [_runtime + "/stdlib.zip", _runtime + "/native"]

import base64
import builtins
import json
import resource
import runpy
import time
import traceback
from typing import Any


class DeviceError(Exception):
    pass


class Robo:
    def __init__(self, config: dict[str, Any]) -> None:
        self.parameters = config["parameters"]
        self.job_id = config["job_id"]
        self.program_version = config["program_version"]

    def _request(self, operation: str, **args: Any) -> dict[str, Any]:
        assert sys.__stdout__ is not None and sys.__stdin__ is not None
        sys.__stdout__.write(json.dumps({"op": operation, "args": args}) + "\n")
        sys.__stdout__.flush()
        line = sys.__stdin__.readline(2 * 1024 * 1024)
        if not line:
            raise DeviceError("Execution host disconnected; no further device access")
        reply = json.loads(line)
        if "error" in reply:
            raise DeviceError(reply["error"] + ": " + reply.get("message", ""))
        return dict(reply["result"])

    def discover(self) -> dict[str, Any]:
        return self._request("devices")

    def observe(self) -> dict[str, Any]:
        return self._request("observe")

    def move(
        self,
        dx: float,
        dy: float,
        *,
        based_on: str,
        command_id: str,
        speed: float = 5,
        lose_ack: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "move",
            dx=dx,
            dy=dy,
            speed=speed,
            based_on=based_on,
            command_id=command_id,
            lose_ack=lose_ack,
        )

    def query(self, command_id: str) -> dict[str, Any]:
        return self._request("query", command_id=command_id)

    def log(self, text: str) -> dict[str, Any]:
        return self._request("log", text=str(text))

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def centroid(observation: dict[str, Any]) -> tuple[float, float]:
        """Local red marker perception from camera pixels, no model or telemetry."""
        frame = observation["pixels"]
        pixels = base64.b64decode(frame["rgb"])
        coordinates = [
            (i // 3 % frame["width"], i // 3 // frame["width"])
            for i in range(0, len(pixels), 3)
            if pixels[i] > 180 and pixels[i + 1] < 80 and pixels[i + 2] < 80
        ]
        if len(coordinates) < 9:
            raise DeviceError("Marker lost: local perception cannot continue")
        return tuple(
            sum(point[i] for point in coordinates) / len(coordinates) for i in (0, 1)
        )


def main() -> None:
    config = json.loads(sys.argv[1])
    limits = config["limits"]
    resource.setrlimit(
        resource.RLIMIT_CPU, (limits["cpu_seconds"], limits["cpu_seconds"])
    )
    # macOS rejects RLIMIT_AS below its shared-cache virtual address space.
    # The independent supervisor enforces the configured resident-memory cap.
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    sys.path[:] = [
        config["archive"],
        config["runtime"] + "/stdlib.zip",
        config["runtime"] + "/native",
    ]
    robo = Robo(config)
    builtins.robo = robo  # type: ignore[attr-defined]

    def log_print(*args: Any, **kwargs: Any) -> None:
        robo.log(" ".join(str(arg) for arg in args))

    builtins.print = log_print
    try:
        runpy.run_module(
            config["entrypoint"][:-3].replace("/", "."), run_name="__main__"
        )
    except BaseException:
        robo.log(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
