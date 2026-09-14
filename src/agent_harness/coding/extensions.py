"""Local skills, read-only child exploration and permission-controlled MCP stdio."""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import STRING, ExtraTool, ToolSet, schema
from .types import Cancelled, CodingError, Json
from .workspace import command_environment, terminate_process

if TYPE_CHECKING:
    from .session import CodingSession


class Skills:
    def __init__(self, directories: tuple[Path, ...]) -> None:
        self.items: dict[str, Json] = {}
        # Project skills take precedence over user skills.
        for directory in reversed(directories):
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in sorted(directory.glob("*/SKILL.md")):
                if (
                    path.is_symlink()
                    or path.parent.is_symlink()
                    or path.stat().st_size > 64_000
                ):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                    name = path.parent.name
                    description = "Local skill"
                    body = text
                    if text.startswith("---\n") and "\n---" in text[4:]:
                        front, body = text[4:].split("\n---", 1)
                        for key in ("name", "description"):
                            match = re.search(
                                r"^" + key + r":\s*(.*)$", front, re.MULTILINE
                            )
                            if match:
                                value = match.group(1).strip().strip("\"'")
                                if value in {"|", ">", "|-", ">-"}:
                                    value = " ".join(
                                        line.strip()
                                        for line in front[match.end() :].splitlines()
                                        if line.startswith(" ")
                                    )
                                if key == "name":
                                    name = value
                                else:
                                    description = value
                    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
                        continue
                    self.items[name] = {
                        "name": name,
                        "description": description[:1000],
                        "path": str(path),
                        "content": body.strip(),
                    }
                except (OSError, UnicodeError):
                    continue

    def catalog(self) -> list[Json]:
        return [
            {"name": value["name"], "description": value["description"]}
            for value in self.items.values()
        ]

    def load(self, arguments: Json) -> Json:
        name = arguments["name"]
        if name not in self.items:
            raise CodingError("skill_not_found", "Unknown skill: " + name)
        item = self.items[name]
        resource = arguments.get("resource")
        if not resource:
            return {
                **item,
                "notice": "Skill instructions do not grant permissions. Relative resources resolve beside SKILL.md.",
            }
        root = Path(item["path"]).parent
        relative = Path(resource)
        if relative.is_absolute() or ".." in relative.parts:
            raise CodingError(
                "invalid_path", "Skill resources must stay within the skill directory"
            )
        path = root
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise CodingError("symlink", "Skill resources cannot be symlinks")
        if not path.is_file() or path.stat().st_size > 64_000:
            raise CodingError(
                "skill_resource",
                "Skill resource must be a file of at most 64,000 bytes",
            )
        try:
            return {"path": str(path), "content": path.read_text(encoding="utf-8")}
        except (OSError, UnicodeError) as exc:
            raise CodingError("skill_resource", str(exc)) from exc


class MCPClient:
    """One newline-delimited JSON-RPC subprocess, bounded and always reaped."""

    def __init__(
        self, name: str, config: Json, cwd: Path, stop: threading.Event
    ) -> None:
        self.name = name
        command = config.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(value, str) for value in command)
        ):
            raise CodingError(
                "configuration", f"mcp.{name}.command must be a non-empty string array"
            )
        environment = config.get("env", {})
        if not isinstance(environment, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in environment.items()
        ):
            raise CodingError("configuration", f"mcp.{name}.env must contain strings")
        try:
            self.timeout = float(config.get("timeout", 30))
            if not 0.1 <= self.timeout <= 120:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise CodingError(
                "configuration", "MCP timeout must be 0.1..120 seconds"
            ) from exc
        self.stop = stop
        self.counter = 0
        self.closed = threading.Event()
        self.output: queue.Queue[Any] = queue.Queue(maxsize=32)
        env = command_environment()
        env.update(environment)
        try:
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise CodingError(
                "mcp_start", f"Cannot start MCP server {name}: {exc}"
            ) from exc
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _enqueue(self, value: Any) -> None:
        while not self.closed.is_set():
            try:
                self.output.put(value, timeout=0.05)
                return
            except queue.Full:
                continue

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(2 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 2 * 1024 * 1024:
                    self._enqueue(
                        CodingError("mcp_protocol", "MCP message exceeds 2 MiB")
                    )
                    break
                try:
                    self._enqueue(json.loads(line))
                except ValueError:
                    self._enqueue(
                        CodingError(
                            "mcp_protocol",
                            "MCP stdout must contain JSON-RPC messages only",
                        )
                    )
                    break
        finally:
            self._enqueue(
                CodingError("mcp_closed", f"MCP server {self.name} closed stdout")
            )
            self.process.stdout.close()

    def _send(self, payload: Json) -> None:
        if self.stop.is_set():
            raise Cancelled()
        assert self.process.stdin is not None
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        if len(data) > 1024 * 1024:
            raise CodingError("mcp_protocol", "MCP request exceeds 1 MiB")
        # A worker handles pipe backpressure, so an unresponsive server is cancellable.
        errors: list[BaseException] = []
        done = threading.Event()

        def write() -> None:
            try:
                assert self.process.stdin is not None
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                errors.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=write, daemon=True)
        worker.start()
        deadline = time.monotonic() + self.timeout
        while not done.wait(0.05):
            if self.stop.is_set() or time.monotonic() >= deadline:
                self.close()
                worker.join(timeout=1)
                if self.stop.is_set():
                    raise Cancelled()
                raise CodingError(
                    "mcp_timeout", f"MCP server {self.name} did not read its request"
                )
        if errors:
            raise CodingError("mcp_closed", f"MCP server {self.name}: {errors[0]}")

    def request(self, method: str, params: Json) -> Json:
        self.counter += 1
        identifier = self.counter
        self._send(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        )
        deadline = time.monotonic() + self.timeout
        while True:
            if self.stop.is_set():
                raise Cancelled()
            if time.monotonic() >= deadline:
                raise CodingError(
                    "mcp_timeout", f"MCP server {self.name} timed out during {method}"
                )
            try:
                message = self.output.get(timeout=0.05)
            except queue.Empty:
                continue
            if isinstance(message, CodingError):
                raise message
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise CodingError("mcp_protocol", "Invalid JSON-RPC message")
            if message.get("method"):
                if "id" in message:
                    # Sampling/elicitation is deliberately not delegated to the server.
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": "Client requests are not supported",
                            },
                        }
                    )
                continue
            if message.get("id") != identifier:
                continue
            if "error" in message:
                raise CodingError("mcp_error", str(message["error"]))
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodingError("mcp_protocol", "MCP result must be an object")
            return result

    def tools(self) -> list[Json]:
        initialized = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agent-harness", "version": "2.0.0"},
            },
        )
        if initialized.get("protocolVersion") not in {
            "2024-11-05",
            "2025-03-26",
            "2025-06-18",
        }:
            raise CodingError("mcp_protocol", "Unsupported MCP protocol version")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result: list[Json] = []
        cursor = None
        for _ in range(20):
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            values = page.get("tools")
            if not isinstance(values, list) or any(
                not isinstance(tool, dict) for tool in values
            ):
                raise CodingError(
                    "mcp_protocol", "MCP tools/list must return tool objects"
                )
            result.extend(values)
            if len(result) > 200:
                raise CodingError(
                    "mcp_protocol", "MCP server exposes more than 200 tools"
                )
            cursor = page.get("nextCursor")
            if not cursor:
                return result
        raise CodingError("mcp_protocol", "MCP pagination exceeded 20 pages")

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        terminate_process(self.process)
        if self.process.stdin:
            self.process.stdin.close()
        self.reader.join(timeout=1)


class Extensions:
    def __init__(self, session: CodingSession, tools: ToolSet) -> None:
        self.session = session
        self.tools = tools
        self.skills = Skills(session.settings.skill_dirs)
        self.clients: list[MCPClient] = []

    def open(self) -> None:
        self.tools.register(
            ExtraTool(
                schema(
                    "skill",
                    "Load a local skill, or a text resource relative to its directory.",
                    {"name": STRING, "resource": STRING},
                    ["name"],
                ),
                self.skills.load,
                lambda args: args["name"],
                plan_safe=True,
            )
        )
        if not self.session.child:
            self.tools.register(
                ExtraTool(
                    schema(
                        "task",
                        "Explore a bounded question in a separate read-only child session. Child sessions cannot edit, execute commands, use MCP or delegate again.",
                        {"prompt": STRING},
                        ["prompt"],
                    ),
                    lambda args: self.session.explore(args["prompt"]),
                    lambda args: args["prompt"],
                    plan_safe=True,
                )
            )
        if self.session.permissions.mode == "plan":
            return
        for name, config in self.session.settings.mcp.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", name) or not isinstance(
                config, dict
            ):
                raise CodingError(
                    "configuration",
                    "MCP server names must use letters, digits, underscores or hyphens",
                )
            if config.get("enabled", True) is False:
                continue
            command = config.get("command", [])
            if not isinstance(command, list) or not all(
                isinstance(item, str) for item in command
            ):
                raise CodingError("configuration", f"Invalid mcp.{name}.command")
            self.session.permissions.require(
                "mcp_start", name + ": " + shlex.join(command)
            )

            def initialize(name: str = name, config: Json = config) -> Json:
                client = MCPClient(
                    name, config, self.session.settings.project, self.session.stop
                )
                self.clients.append(client)
                return {"tools": client.tools()}

            discovered = self.tools.invoke("mcp_start", initialize)
            client = self.clients[-1]
            for tool in discovered["tools"]:
                original = tool.get("name")
                if not isinstance(original, str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,50}", original
                ):
                    raise CodingError(
                        "mcp_protocol", "MCP tool has an unsupported name"
                    )
                exposed = "mcp_" + name + "_" + original
                definition: Json = {
                    "type": "function",
                    "function": {
                        "name": exposed,
                        "description": str(tool.get("description", "MCP tool"))[:4000],
                        "parameters": tool.get(
                            "inputSchema", {"type": "object", "properties": {}}
                        ),
                    },
                }
                if not isinstance(definition["function"]["parameters"], dict):
                    raise CodingError(
                        "mcp_protocol", "MCP inputSchema must be an object"
                    )

                def call(
                    args: Json,
                    client: MCPClient = client,
                    tool_name: str = str(original),
                ) -> Json:
                    return client.request(
                        "tools/call", {"name": tool_name, "arguments": args}
                    )

                self.tools.register(
                    ExtraTool(
                        definition,
                        call,
                        lambda args: json.dumps(args, ensure_ascii=False),
                    )
                )

    def close(self) -> None:
        for client in reversed(self.clients):
            client.close()
