from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from vibe import __version__
from vibe.app_server.local import ClientDescriptor, LocalHarnessOptions
from vibe.app_server.protocol import ClientInfo, SessionOptions
from vibe.capx.app import create_capx_app
from vibe.capx.harness_agent import CapXHarnessAgent


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="robo-capx-bridge",
        description="Expose Robo as an OpenAI-compatible CaP-X coding agent.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8110)
    parser.add_argument("-C", "--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--trace-dir", type=Path)
    parser.add_argument("--api-key-env", default="ROBO_CAPX_API_KEY")
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--trust", action="store_true")
    parser.add_argument("--experimental-harness", action="store_true")
    args = parser.parse_args()

    cwd = args.cwd.expanduser().resolve()
    trace_dir = (args.trace_dir or cwd / ".vibe" / "capx-runs").resolve()
    options = LocalHarnessOptions(
        experimental_harness=args.experimental_harness,
        client=ClientDescriptor(
            info=ClientInfo(
                name="robo_capx_bridge",
                title="Robo CaP-X Bridge",
                version=__version__,
                entrypoint="programmatic",
            )
        ),
        session_options=SessionOptions(
            cwd=str(cwd),
            workspace_roots=[str(cwd)],
            enabled_tools=["read_file", "grep"],
            headless=True,
            trust_workspace=args.trust,
        ),
    )
    agent = CapXHarnessAgent.local(options, max_concurrency=args.max_concurrency)
    app = create_capx_app(
        agent, trace_dir=trace_dir, api_key=os.getenv(args.api_key_env)
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
