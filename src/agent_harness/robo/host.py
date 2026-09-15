"""Standalone execution host. No model, conversation or MCP lifecycle ownership."""

from __future__ import annotations

import json
import os
import signal
import socketserver
import threading
from pathlib import Path

from filelock import FileLock, Timeout

from agent_harness.coding.types import CodingError, Json

from .client import MAX_WIRE, socket_path
from .runtime import Runtime


def serve(root: Path, *, backend: str = "sim") -> None:
    root = root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with FileLock(str(root / "host.lock"), timeout=0):
            runtime = Runtime(root, backend=backend)
            path = socket_path(root)
            path.unlink(missing_ok=True)

            class Handler(socketserver.StreamRequestHandler):
                timeout = 20

                def handle(self) -> None:
                    try:
                        raw = self.rfile.readline(MAX_WIRE + 1)
                        if len(raw) > MAX_WIRE or not raw.endswith(b"\n"):
                            raise ValueError("Invalid frame")
                        data = json.loads(raw)
                        if data["protocol"] != 1 or not isinstance(data["args"], dict):
                            raise ValueError("Invalid protocol")
                        value: Json = {
                            "result": runtime.request(data["op"], data["args"])
                        }
                    except CodingError as exc:
                        value = {"error": exc.code, "message": str(exc)}
                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                        OSError,
                        SyntaxError,
                    ) as exc:
                        value = {"error": "invalid_request", "message": str(exc)}
                    try:
                        encoded = (json.dumps(value, allow_nan=False) + "\n").encode()
                        if len(encoded) > MAX_WIRE:
                            encoded = b'{"error":"response_limit","message":"Use a smaller event page"}\n'
                        self.wfile.write(encoded)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # Results stay durable even when the client leaves.

            class Server(socketserver.ThreadingUnixStreamServer):
                daemon_threads = True

            server = Server(str(path), Handler)
            os.chmod(path, 0o600)

            def shutdown(signum: int, frame: object) -> None:
                threading.Thread(target=server.shutdown, daemon=True).start()

            previous = {
                sig: signal.signal(sig, shutdown)
                for sig in (signal.SIGINT, signal.SIGTERM)
            }
            try:
                print(
                    json.dumps(
                        {
                            "status": "ready",
                            "backend": backend,
                            "root": str(root),
                            "socket": str(path),
                        }
                    ),
                    flush=True,
                )
                server.serve_forever(poll_interval=0.1)
            finally:
                runtime.close()
                server.server_close()
                path.unlink(missing_ok=True)
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    except Timeout as exc:
        raise CodingError(
            "host_busy", "A Robo host already owns this data directory"
        ) from exc
