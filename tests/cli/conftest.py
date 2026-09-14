from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest


@dataclass
class Reply:
    body: Any = None
    status: int = 200
    chunks: list[bytes] = field(default_factory=list)
    delay_headers: float = 0
    delay_chunks: float = 0
    content_type: str = "application/json"


def answer(text: str = "Done", calls: list[dict] | None = None, **kwargs: Any) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": text,
                    **({"tool_calls": calls} if calls else {}),
                    **kwargs,
                },
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def call(name: str, arguments: dict, id: str = "call-1") -> dict:
    return {
        "id": id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def sse(*payloads: Any) -> Reply:
    return Reply(
        chunks=[
            (
                "data: "
                + (value if isinstance(value, str) else json.dumps(value))
                + "\n\n"
            ).encode()
            for value in payloads
        ],
        content_type="text/event-stream",
    )


@pytest.fixture
def model_server():
    active = []

    def create(responses):
        requests = []
        headers = []
        errors = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond(Reply(body={"data": [{"id": "test-model"}]}))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                headers.append(dict(self.headers))
                try:
                    value = (
                        responses(body, len(requests) - 1)
                        if callable(responses)
                        else responses[len(requests) - 1]
                    )
                except (
                    AssertionError,
                    IndexError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as exc:
                    errors.append(exc)
                    value = Reply(body={"error": str(exc)}, status=400)
                self.respond(value if isinstance(value, Reply) else Reply(body=value))

            def respond(self, reply):
                try:
                    time.sleep(reply.delay_headers)
                    self.send_response(reply.status)
                    self.send_header("Content-Type", reply.content_type)
                    self.end_headers()
                    if reply.chunks:
                        for chunk in reply.chunks:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            time.sleep(reply.delay_chunks)
                    else:
                        self.wfile.write(json.dumps(reply.body).encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        worker.start()
        server.daemon_threads = True
        active.append((server, worker, errors))
        return f"http://127.0.0.1:{server.server_port}/v1", requests, headers

    yield create
    for server, worker, errors in active:
        server.shutdown()
        server.server_close()
        worker.join(1)
        assert not errors, errors


@pytest.fixture
def cli_env(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENT_HARNESS_")
    }
    env.update(
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        AGENT_HARNESS_DATA_DIR=str(tmp_path / "data"),
        TERM="dumb",
        NO_COLOR="1",
        PROMPT_TOOLKIT_NO_CPR="1",
    )
    return project, env
