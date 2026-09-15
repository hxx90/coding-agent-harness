"""Short local requests and resumable event subscriptions to a standalone host."""

from __future__ import annotations

import hashlib
import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path

from agent_harness.coding.types import CodingError, Json

from .runtime import TERMINAL

MAX_WIRE = 4 * 1024 * 1024


def socket_path(root: Path) -> Path:
    # macOS AF_UNIX paths are limited to 104 bytes. Private per-user directory.
    directory = Path("/tmp") / ("robo-" + str(os.getuid()))
    directory.mkdir(mode=0o700, exist_ok=True)
    if (
        directory.is_symlink()
        or directory.stat().st_uid != os.getuid()
        or directory.stat().st_mode & 0o077
    ):
        raise CodingError(
            "socket_permissions", "Robo socket directory must be owner-only"
        )
    return directory / (
        hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:32] + ".sock"
    )


class Client:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def request(self, op: str, **args: object) -> Json:
        data = (
            json.dumps({"protocol": 1, "op": op, "args": args}, allow_nan=False) + "\n"
        ).encode()
        if len(data) > MAX_WIRE:
            raise CodingError("request_limit", "Robo request exceeds 4 MiB")
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(20)
                connection.connect(str(socket_path(self.root)))
                connection.sendall(data)
                with connection.makefile("rb") as reader:
                    line = reader.readline(MAX_WIRE + 1)
                    if len(line) > MAX_WIRE or not line.endswith(b"\n"):
                        raise ValueError("invalid or incomplete host response")
                    response = json.loads(line)
        except (OSError, ValueError) as exc:
            raise CodingError(
                "host_unavailable",
                "Robo host is unavailable or response was lost. Start/reconnect the host; for submissions use lookup with the original request_id before retrying.",
            ) from exc
        if "error" in response:
            raise CodingError(
                response["error"], response.get("message", response["error"])
            )
        return dict(response["result"])

    def subscribe(self, job_id: str, after: int = 0) -> Iterator[Json]:
        while True:
            value = self.request("events", job_id=job_id, after=after, wait=1)
            yield value
            after = value["cursor"]
            if value["job"]["status"] in TERMINAL and not value["events"]:
                return
