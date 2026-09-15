"""Robo's device/program/job CLI, sharing the existing agent CLI when requested."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent_harness.coding.types import CodingError

from . import __version__
from .client import Client
from .hardware import MHS_STATUS
from .host import serve
from .programs import source_bundle


def default_root() -> Path:
    return (
        Path(os.environ.get("ROBO_HOME", "~/.local/share/agent-harness/robo"))
        .expanduser()
        .resolve()
    )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    agent_arguments = arguments
    agent_home = default_root()
    if len(arguments) >= 2 and arguments[0] == "--home":
        agent_home = Path(arguments[1]).expanduser().resolve()
        agent_arguments = arguments[2:]
    elif arguments and arguments[0].startswith("--home="):
        agent_home = Path(arguments[0].split("=", 1)[1]).expanduser().resolve()
        agent_arguments = arguments[1:]
    if agent_arguments and agent_arguments[0] in {"run", "chat"}:
        from agent_harness.cli import main as agent_main

        return agent_main([*agent_arguments, "--robo-home", str(agent_home)])
    parser = argparse.ArgumentParser(
        prog="robo",
        description="Robo — A runtime for agents that act in the physical world",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=default_root(),
        help="Persistent host directory (or ROBO_HOME)",
    )
    parser.add_argument("--version", action="version", version="Robo " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "run", help="Run an agent task with Robo tools (provider options follow run)"
    )
    sub.add_parser(
        "chat", help="Interactive agent with Robo tools (provider options follow chat)"
    )
    host = sub.add_parser(
        "host", help="Run the independent execution host in this terminal"
    )
    host.add_argument("--backend", choices=["sim", "mhs"], default="sim")
    for name in ("devices", "health", "mhs-status", "jobs", "protect"):
        sub.add_parser(name)
    observation = sub.add_parser("observe")
    observation.add_argument("--job-id")
    publish = sub.add_parser("publish")
    publish.add_argument("directory", type=Path)
    publish.add_argument("--manifest", default="program.json")
    submit = sub.add_parser("submit")
    submit.add_argument("program_version")
    submit.add_argument("--request-id", required=True)
    submit.add_argument("--based-on", required=True)
    submit.add_argument("--parameters", default="{}", help="JSON object")
    submit.add_argument(
        "--disconnect-policy",
        choices=["continue_bounded", "cancel"],
        default="continue_bounded",
    )
    for name in ("status", "cancel", "disconnect", "events", "action"):
        cmd = sub.add_parser(name)
        cmd.add_argument("job_id")
        if name == "action":
            cmd.add_argument("command_id")
        if name == "events":
            cmd.add_argument("--after", type=int, default=0)
            cmd.add_argument("--follow", action="store_true")
    lookup = sub.add_parser("lookup")
    lookup.add_argument("request_id")
    sub.add_parser(
        "demo",
        help="Run a wrong-direction trial, edit and rerun the local visual controller",
    )
    args = parser.parse_args(arguments)
    try:
        if args.command == "host":
            serve(args.home, backend=args.backend)
            return 0
        if args.command == "mhs-status":
            print(json.dumps(MHS_STATUS, ensure_ascii=False))
            return 0
        if args.command == "demo":
            from .demo import demo

            outcome = demo(args.home)
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
            return 0 if outcome["passed"] else 1
        client = Client(args.home)
        params = {
            k: v
            for k, v in vars(args).items()
            if k not in {"home", "command", "follow"} and v is not None
        }
        if args.command == "publish":
            params = source_bundle(args.directory, args.directory / args.manifest)
        elif args.command == "submit":
            params["parameters"] = json.loads(args.parameters)
        elif args.command == "events" and args.follow:
            for update in client.subscribe(args.job_id, args.after):
                print(json.dumps(update, ensure_ascii=False), flush=True)
            return 0
        print(
            json.dumps(
                client.request(args.command, **params), ensure_ascii=False, indent=2
            )
        )
        return 0
    except (CodingError, ValueError, OSError) as exc:
        print(
            json.dumps(
                {"error": getattr(exc, "code", "usage"), "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
