"""Short local requests and resumable event subscriptions to a standalone host."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

from agent_harness.coding.provider import interruptible
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
    def __init__(self, root: Path, stop: threading.Event | None = None) -> None:
        self.root = root.resolve()
        self.stop = stop or threading.Event()

    def request(self, op: str, **args: object) -> Json:
        data = (
            json.dumps({"protocol": 1, "op": op, "args": args}, allow_nan=False) + "\n"
        ).encode()
        if len(data) > MAX_WIRE:
            raise CodingError("request_limit", "Robo request exceeds 4 MiB")

        async def exchange() -> Json:
            async with asyncio.timeout(20):
                reader, writer = await asyncio.open_unix_connection(
                    str(socket_path(self.root)), limit=MAX_WIRE
                )
                try:
                    writer.write(data)
                    await writer.drain()
                    line = await reader.readline()
                    if len(line) > MAX_WIRE or not line.endswith(b"\n"):
                        raise ValueError("invalid or incomplete host response")
                    return dict(json.loads(line))
                finally:
                    writer.close()
                    await writer.wait_closed()

        try:
            response = asyncio.run(interruptible(exchange(), self.stop))
        except (OSError, ValueError) as exc:
            raise CodingError(
                "host_unavailable",
                "Robo host is unavailable or response was lost. Start/reconnect the host; for submissions use lookup with the original request_id before retrying.",
            ) from exc
        if "error" in response:
            raise CodingError(
                str(response["error"]), str(response.get("message", response["error"]))
            )
        return dict(response["result"])

    def subscribe(self, job_id: str, after: int = 0) -> Iterator[Json]:
        while True:
            value = self.request("events", job_id=job_id, after=after, wait=1)
            yield value
            after = value["cursor"]
            if value["job"]["status"] in TERMINAL and not value["events"]:
                return
