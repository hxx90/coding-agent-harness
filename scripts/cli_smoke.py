"""Exercise an installed CLI against a real loopback model protocol service.

This deterministic fixture checks transport and tool execution, not model ability.
Run: python scripts/cli_smoke.py --executable /path/to/agent-harness
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="agent-harness-cli-smoke-"))
    project = root / "project"
    project.mkdir()
    (project / "counter.py").write_text("value = 1\n")
    (project / "test_counter.py").write_text(
        "import unittest\nfrom counter import value\nclass CounterTest(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value, 2)\n"
    )
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            index = len(requests)
            message: dict[str, Any] = {
                "role": "assistant",
                "content": "Updated counter and ready for host validation.",
            }
            if index <= 2:
                name, arguments = (
                    ("read", {"path": "counter.py"})
                    if index == 1
                    else (
                        "edit",
                        {
                            "path": "counter.py",
                            "old_string": "value = 1",
                            "new_string": "value = 2",
                        },
                    )
                )
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"smoke-{index}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            payload = {
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls" if index <= 2 else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AGENT_HARNESS_")
    }
    env.update(
        AGENT_HARNESS_DATA_DIR=str(root / "data"), XDG_CONFIG_HOME=str(root / "config")
    )
    command = [
        str(Path(args.executable).resolve()),
        "-C",
        str(project),
        "run",
        "Change counter value from 1 to 2 and verify it",
        "--base-url",
        f"http://127.0.0.1:{server.server_port}/v1",
        "--model",
        "protocol-fixture",
        "--allow",
        "edit",
        "--allow",
        "test",
        "--test-command",
        "python -m unittest discover -v",
        "--json",
    ]
    try:
        result = subprocess.run(
            command, env=env, text=True, capture_output=True, timeout=20, check=False
        )
        (root / "events.jsonl").write_text(result.stdout)
        (root / "stderr.txt").write_text(result.stderr)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        final = events[-1]
        assert final["status"] == "completed", final
        assert final["verification"] == "passed", final
        assert (project / "counter.py").read_text() == "value = 2\n"
        assert len(requests) == 3, requests
        print(
            json.dumps(
                {
                    "fixture": "real loopback HTTP, deterministic responses",
                    "workspace": str(project),
                    "model_requests": len(requests),
                    "session_id": final["session_id"],
                    "status": final["status"],
                    "verification": final["verification"],
                    "file": "counter.py",
                    "before": "value = 1",
                    "after": "value = 2",
                    "events": str(root / "events.jsonl"),
                },
                indent=2,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(1)


if __name__ == "__main__":
    main()
