"""Small terminal UI and script commands over the coding session interface."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import select
import shlex
import shutil
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console

from agent_harness import __version__
from agent_harness.trace import Redactor

from .coding.extensions import Skills
from .coding.provider import Provider
from .coding.session import CodingSession
from .coding.settings import Settings, credential_path, credentials, load_settings
from .coding.storage import SessionStore, atomic_json
from .coding.types import Cancelled, CodingError, Json, RunResult

SLASH_HELP = """/help                 Show commands
/new                  Start a new session
/sessions             List this project's sessions
/resume ID            Resume a saved session
/status               Session, model, mode and usage
/mode plan|build      Switch execution mode
/model ID             Switch model
/attach PATH          Attach a project text file to the next message
/diff                 Show the latest turn's changes
/undo, /redo          Revert or restore a turn and its conversation
/compact              Compact the next model request
/todos                Show the task checklist
/skills               List local skills
/export PATH          Export the conversation
/exit                 Leave the CLI (also Ctrl+D)
Ctrl+C                Cancel the active request/command; keep the session"""


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument(
        "-C", "--project", help="Project directory (default: current directory)"
    )
    common.add_argument(
        "--base-url", help="Chat Completions API base URL, usually ending in /v1"
    )
    common.add_argument("--model")
    common.add_argument("--profile")
    common.add_argument(
        "--api-key-env", help="Environment variable containing the API key"
    )
    common.add_argument("--data-dir")
    common.add_argument(
        "--robo-home", help="Connect Robo tools to an independent execution host"
    )
    common.add_argument("--mode", choices=["plan", "build"])
    common.add_argument("--max-turns", type=int)
    common.add_argument("--max-tokens", type=int)
    common.add_argument("--max-seconds", type=float)
    common.add_argument("--timeout", type=float, help="Model HTTP timeout in seconds")
    common.add_argument(
        "--test-command",
        help="Owner validation command, parsed as argv without a shell",
    )
    common.add_argument(
        "--allow",
        action="append",
        help="Allow a tool or wildcard; repeat for multiple tools",
    )
    common.add_argument("--deny", action="append", help="Deny a tool or wildcard")
    common.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Approve trusted local execution; explicit deny rules and Plan restrictions still apply",
    )
    common.add_argument("--no-stream", action="store_false", dest="stream")
    common.add_argument(
        "--json", action="store_true", help="Write JSONL events or JSON command results"
    )
    common.add_argument("--session", help="Resume a session ID/prefix or latest")
    root = argparse.ArgumentParser(
        prog="agent-harness",
        description="A local coding agent. No command opens the interactive terminal; piped stdin runs one task.",
        parents=[common],
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command")
    run = commands.add_parser("run", parents=[common], help="Run one task and exit")
    run.add_argument("prompt", nargs="*", help="Task text (use - or pipe stdin)")
    run.add_argument("--attach", action="append", default=[])
    commands.add_parser("chat", parents=[common], help="Interactive terminal")
    commands.add_parser(
        "models", parents=[common], help="List models from the configured service"
    )
    commands.add_parser(
        "doctor",
        parents=[common],
        help="Check local setup and configured model service",
    )
    commands.add_parser("skills", parents=[common], help="List local skills")
    config = commands.add_parser(
        "config", parents=[common], help="Inspect configuration"
    )
    config.add_argument("action", choices=["show", "path"], default="show", nargs="?")
    auth = commands.add_parser(
        "auth", parents=[common], help="Manage private local credentials"
    )
    auth.add_argument("action", choices=["login", "logout", "status"])
    auth.add_argument(
        "--stdin",
        action="store_true",
        help="Read a login key from stdin instead of a hidden prompt",
    )
    sessions = commands.add_parser(
        "sessions", parents=[common], help="Manage durable conversations"
    )
    sessions.add_argument(
        "action",
        choices=["list", "show", "export", "import", "delete"],
        default="list",
        nargs="?",
    )
    sessions.add_argument("arguments", nargs="*")
    sessions.add_argument(
        "--all", action="store_true", help="List sessions across projects"
    )
    for name in ("diff", "undo", "redo"):
        cmd = commands.add_parser(
            name, parents=[common], help=name.capitalize() + " a session's latest turn"
        )
        cmd.add_argument("id", nargs="?", default="latest")
    return root


def visible(value: Any) -> Any:
    """Public conversation output excludes private reasoning and file snapshots."""
    if isinstance(value, dict):
        return {
            key: visible(item)
            for key, item in value.items()
            if key
            not in {
                "reasoning_content",
                "reasoning_details",
                "active",
                "pending",
                "turns",
                "redo",
            }
        }
    if isinstance(value, list):
        return [visible(item) for item in value]
    return value


class Renderer:
    def __init__(self, json_output: bool) -> None:
        self.json = json_output
        self.console = Console(stderr=True, highlight=False, markup=False)
        self.streaming = False

    def output(self, value: Any) -> None:
        if isinstance(value, str) and not self.json:
            print(value, flush=True)
        else:
            print(json.dumps(value, ensure_ascii=False, default=str), flush=True)

    def event(self, payload: Json) -> None:
        if self.json:
            self.output(payload)
            return
        kind = payload["type"]
        if kind == "text_delta":
            sys.stdout.write(payload["text"])
            sys.stdout.flush()
            self.streaming = True
        else:
            if self.streaming:
                print(flush=True)
                self.streaming = False
            if kind == "tool_started":
                self.console.print(f"→ {payload['tool']} {payload['arguments'][:200]}")
            elif kind == "tool_finished" and payload["result"].get("error"):
                self.console.print(
                    f"  {payload['result']['error']}: {payload['result'].get('message', '')}"
                )
            elif kind == "error":
                self.console.print(f"{payload['code']}: {payload['message']}")
            elif kind == "result":
                self.console.print(
                    f"[{payload['status']}] session={payload['session_id'][:12]} verification={payload['verification']} requests={payload['turns']}"
                )
            elif kind in {
                "recovered",
                "context_compacted",
                "validation_started",
                "validation_finished",
            }:
                self.console.print(
                    kind
                    + ": "
                    + json.dumps(
                        {
                            key: value
                            for key, value in payload.items()
                            if key not in {"type", "timestamp", "session_id"}
                        },
                        ensure_ascii=False,
                    )[:1500]
                )

    def approve(self, tool: str, target: str, stop: threading.Event) -> str:
        self.console.print(f"Permission required — {tool}\n{target}")
        reply = (
            read_input(
                "Allow [y] once / [s] this exact operation for session / [n] deny? ",
                stop,
            )
            .strip()
            .lower()
        )
        return {"y": "once", "s": "session"}.get(reply, "deny")


def read_input(prompt: str, stop: threading.Event) -> str:
    if os.name != "posix":
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            return input(prompt)
        finally:
            signal.signal(signal.SIGINT, previous)
    print(prompt, end="", flush=True)
    data = bytearray()
    while not stop.is_set():
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not ready:
            continue
        value = os.read(sys.stdin.fileno(), 1)
        if not value:
            raise Cancelled("Terminal input closed")
        if value == b"\n":
            return data.decode("utf-8", errors="replace")
        data.extend(value)
        if len(data) > 64_000:
            raise CodingError("input_limit", "Interactive answer exceeds 64,000 bytes")
    raise Cancelled()


@contextmanager
def cancellation(stop: threading.Event) -> Iterator[None]:
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda _signal, _frame: stop.set())
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def run(
    session: CodingSession, prompt: str, attachments: tuple[str, ...] = ()
) -> RunResult:
    session.stop.clear()
    with cancellation(session.stop):
        return session.run(prompt, attachments)


def make_session(
    settings: Settings,
    renderer: Renderer,
    session_id: str | None = None,
    *,
    interactive: bool = False,
) -> CodingSession:
    stop = threading.Event()
    return CodingSession(
        settings,
        session_id=session_id,
        sink=renderer.event,
        stop=stop,
        approve=(lambda tool, target: renderer.approve(tool, target, stop))
        if interactive
        else None,
        question=(lambda question: read_input(question + " ", stop))
        if interactive
        else None,
    )


def repl(settings: Settings, renderer: Renderer, session_id: str | None) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty() or renderer.json:
        raise CodingError(
            "terminal_required",
            "Interactive chat requires a terminal and text output; use run --json for scripts",
        )
    session = make_session(settings, renderer, session_id, interactive=True)
    prompt: PromptSession[str] = PromptSession(history=InMemoryHistory())
    attachments: list[str] = []
    renderer.console.print(
        f"agent-harness {__version__} · {settings.project}\n/help for commands · session {session.id[:12]}"
    )
    while True:
        try:
            line = prompt.prompt(f"{session.permissions.mode}> ").strip()
            if not line:
                continue
            if not line.startswith("/"):
                run(session, line, tuple(attachments))
                attachments.clear()
                continue
            command, *arguments = shlex.split(line)
            if command in {"/exit", "/quit"}:
                return 0
            if command == "/help":
                renderer.output(SLASH_HELP)
            elif command == "/new":
                session = make_session(settings, renderer, interactive=True)
                renderer.output("Session " + session.id)
            elif command == "/resume":
                if len(arguments) != 1:
                    raise CodingError("usage", "Use /resume ID")
                session = make_session(
                    settings, renderer, arguments[0], interactive=True
                )
                renderer.output("Session " + session.id)
            elif command == "/sessions":
                renderer.output(session.store.list(settings.project))
            elif command == "/status":
                renderer.output(
                    {
                        "session_id": session.id,
                        "mode": session.permissions.mode,
                        "model": settings.provider.model,
                        "usage": session.state["usage"],
                    }
                )
            elif command in {"/mode", "/model"}:
                if len(arguments) != 1 or (
                    command == "/mode" and arguments[0] not in {"plan", "build"}
                ):
                    raise CodingError("usage", "Use /mode plan|build or /model ID")
                settings = (
                    replace(settings, mode=arguments[0])
                    if command == "/mode"
                    else replace(
                        settings,
                        provider=replace(settings.provider, model=arguments[0]),
                    )
                )
                session = make_session(settings, renderer, session.id, interactive=True)
                renderer.output(f"{command[1:]}: {arguments[0]}")
            elif command == "/attach":
                if len(arguments) != 1:
                    raise CodingError("usage", "Use /attach PATH")
                session.workspace.text(arguments[0])
                attachments.append(arguments[0])
                renderer.output("Attached " + arguments[0])
            elif command == "/diff":
                renderer.output(session.diff() or "No changes in the latest turn")
            elif command in {"/undo", "/redo"}:
                renderer.output(
                    session.undo(redo=command == "/redo") or "Conversation restored"
                )
            elif command == "/compact":
                session.compact()
                renderer.output("Next request will use compacted context")
            elif command == "/todos":
                renderer.output(session.state["todos"])
            elif command == "/skills":
                renderer.output(Skills(settings.skill_dirs).catalog())
            elif command == "/export":
                if len(arguments) != 1:
                    raise CodingError("usage", "Use /export PATH")
                session.store.export(session.id, Path(arguments[0]).expanduser())
                renderer.output("Exported " + arguments[0])
            else:
                raise CodingError("usage", "Unknown command; use /help")
        except KeyboardInterrupt:
            renderer.console.print("Input cancelled; /exit to leave")
        except EOFError:
            return 0
        except (CodingError, ValueError) as exc:
            renderer.console.print(str(exc))


def dispatch(args: argparse.Namespace, renderer: Renderer) -> int:
    values = vars(args)
    settings = load_settings(Path(values.get("project", ".")), values)
    redactor = Redactor(
        [settings.provider.api_key] if settings.provider.api_key else []
    )
    command = args.command
    if command == "auth":
        path = credential_path()
        saved = credentials()
        profile = settings.provider.profile
        if args.action == "login":
            key = (
                sys.stdin.readline().strip()
                if args.stdin
                else getpass.getpass(f"API key for {profile}: ").strip()
            )
            if not key:
                raise CodingError("usage", "API key cannot be empty")
            saved[profile] = {"api_key": key}
            atomic_json(path, saved)
            renderer.output({"profile": profile, "saved": True, "path": str(path)})
        elif args.action == "logout":
            saved.pop(profile, None)
            atomic_json(path, saved)
            renderer.output({"profile": profile, "removed": True})
        else:
            renderer.output(
                {
                    "profile": profile,
                    "key_configured": bool(settings.provider.api_key),
                    "stored_profiles": sorted(saved),
                }
            )
        return 0
    if command == "config":
        renderer.output(
            settings.public()
            if args.action == "show"
            else {
                "user": os.environ.get(
                    "AGENT_HARNESS_CONFIG",
                    str(credential_path().with_name("config.toml")),
                ),
                "project": str(settings.project / ".agent-harness.toml"),
                "credentials": str(credential_path()),
                "data": str(settings.data_dir),
            }
        )
        return 0
    if command == "skills":
        renderer.output(Skills(settings.skill_dirs).catalog())
        return 0
    if command in {"models", "doctor"}:
        stop = threading.Event()
        with cancellation(stop):
            if command == "models":
                renderer.output(Provider(settings.provider).models(stop))
                return 0
            report: Json = {
                "python": sys.version.split()[0],
                "project": str(settings.project),
                "git": bool(shutil.which("git")),
                "node": bool(shutil.which("node")),
                "provider_configured": bool(
                    settings.provider.base_url and settings.provider.model
                ),
                "validation_command": list(settings.validation_command),
                "shell_policy": "trusted execution after permission, not an OS sandbox",
            }
            if report["provider_configured"]:
                try:
                    available = Provider(settings.provider).models(stop)
                    report.update(
                        connection="ok",
                        model_listed=settings.provider.model in available,
                    )
                except CodingError as exc:
                    report.update(connection="error", error=redactor.text(str(exc)))
            else:
                report["connection"] = "not_configured"
            renderer.output(report)
            return 0 if report["connection"] == "ok" else 1
    store = SessionStore(settings.data_dir)
    if command == "sessions":
        action = args.action
        expected = {"list": 0, "show": 1, "export": 2, "import": 1, "delete": 1}[action]
        if len(args.arguments) != expected:
            raise CodingError(
                "usage", f"sessions {action} requires {expected} argument(s)"
            )
        if action == "list":
            renderer.output(
                redactor.value(store.list(None if args.all else settings.project))
            )
        elif action == "import":
            state = store.import_session(Path(args.arguments[0]), settings.project)
            renderer.output({"session_id": state["id"]})
        else:
            identifier = store.resolve(args.arguments[0], settings.project)
            if action == "show":
                renderer.output(redactor.value(visible(store.load(identifier))))
            elif action == "export":
                store.export(identifier, Path(args.arguments[1]))
                renderer.output({"exported": args.arguments[1]})
            else:
                store.delete(identifier)
                renderer.output({"deleted": identifier})
        return 0
    if command in {"diff", "undo", "redo"}:
        session = make_session(settings, renderer, values.get("session", args.id))
        renderer.output(
            session.diff()
            if command == "diff"
            else session.undo(redo=command == "redo")
        )
        return 0
    if command == "chat" or (command is None and sys.stdin.isatty()):
        return repl(settings, renderer, values.get("session"))
    prompt = " ".join(values.get("prompt", []))
    if not sys.stdin.isatty():
        piped = sys.stdin.read(256_001)
        if len(piped) > 256_000:
            raise CodingError("input_limit", "stdin exceeds 256,000 characters")
        prompt = (
            ("" if prompt == "-" else prompt)
            + ("\n" if prompt and prompt != "-" and piped else "")
            + piped
        )
    elif prompt == "-":
        raise CodingError("usage", "Use a pipe with run -")
    if not prompt.strip() and not values.get("attach"):
        raise CodingError(
            "empty_prompt", "Provide a task: agent-harness run 'Explain this project'"
        )
    session = make_session(
        settings,
        renderer,
        values.get("session"),
        interactive=sys.stdin.isatty() and not renderer.json,
    )
    return run(session, prompt, tuple(values.get("attach", []))).exit_code


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    renderer = Renderer(getattr(args, "json", False))
    try:
        return dispatch(args, renderer)
    except CodingError as exc:
        renderer.event({"type": "error", "code": exc.code, "message": str(exc)})
        return {
            "cancelled": 130,
            "permission_required": 3,
            "budget_exhausted": 4,
            "usage": 2,
        }.get(exc.code, 1)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except OSError as exc:
        renderer.event({"type": "error", "code": "io_error", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
