"""Durable agent loop. Terminal and script callers use the same CodingSession."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace

from filelock import FileLock, Timeout

from agent_harness.trace import SENSITIVE_NAME_RE, Redactor

from .changes import ChangeJournal
from .context import StreamingRedactor, attach, request_messages, system_prompt
from .permissions import Approval, Permissions
from .provider import Model, Provider
from .settings import Settings
from .storage import SessionStore
from .tools import ToolSet
from .types import Cancelled, CodingError, EventSink, Json, RunResult, event
from .workspace import Workspace


class CodingSession:
    def __init__(
        self,
        settings: Settings,
        *,
        model: Model | None = None,
        session_id: str | None = None,
        sink: EventSink | None = None,
        approve: Approval | None = None,
        question: Callable[[str], str] | None = None,
        stop: threading.Event | None = None,
        child: bool = False,
    ) -> None:
        self.settings = settings
        self.model = model or Provider(settings.provider)
        self.store = SessionStore(settings.data_dir)
        self.state = (
            self.store.load(self.store.resolve(session_id, settings.project))
            if session_id
            else self.store.create(
                settings.project, settings.provider.model, settings.mode
            )
        )
        if self.state["project"] != str(settings.project):
            raise CodingError(
                "project_mismatch",
                f"Session belongs to {self.state['project']}; select that project with -C",
            )
        self.id: str = self.state["id"]
        self.sink = sink or (lambda _: None)
        self.stop = stop if stop is not None else threading.Event()
        self.permissions = Permissions(settings.permission_rules, mode=settings.mode)
        self.question = question
        self.workspace = Workspace(settings)
        self.child = child
        secrets = [settings.provider.api_key]
        for server in settings.mcp.values():
            if isinstance(server, dict) and isinstance(server.get("env"), dict):
                secrets.extend(
                    str(value)
                    for key, value in server["env"].items()
                    if SENSITIVE_NAME_RE.search(key)
                )
        self.redactor = Redactor([secret for secret in secrets if secret])
        if approve:
            self.permissions.approve = lambda tool, preview: approve(
                tool, self.redactor.text(preview)
            )

    def emit(self, kind: str, **data: object) -> None:
        data.setdefault("session_id", self.id)
        payload = event(kind, **data)
        for key in ("text", "message", "arguments", "result", "error"):
            if key in payload:
                payload[key] = self.redactor.value(payload[key])
        self.store.append_event(self.id, payload)
        self.sink(payload)

    def _save(self) -> None:
        self.state["extra_paths"] = sorted(self.workspace.extra_paths)
        # Private provider metadata is retained for continuity, never in UI events.
        self.state["todos"] = self.redactor.value(self.state["todos"])
        self.store.save(self.state)

    def _reload(self) -> None:
        self.state = self.store.load(self.id)
        self.workspace.extra_paths.update(self.state.get("extra_paths", []))

    def _balance_pending(self, reason: str) -> None:
        pending: dict[str, Json] = {}
        for message in self.state["messages"]:
            for call in message.get("tool_calls", []):
                pending[call["id"]] = call
            if message["role"] == "tool":
                pending.pop(message.get("tool_call_id"), None)
        for call_id in pending:
            self.state["messages"].append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        {"error": "interrupted_operation", "message": reason}
                    ),
                }
            )
        self.state.pop("pending", None)

    def _recover(self) -> None:
        if self.state.get("active"):
            self._balance_pending(
                "The previous process stopped. This operation may or may not have executed. Inspect current files; never blindly replay it."
            )
            active = self.state["active"]
            if "before" in active:
                active["effect"] = {"tool": "unknown", "before": active.pop("before")}
            ChangeJournal(self.workspace, self.state, self._save).finish(uncertain=True)
            self._finish_turn("interrupted")
            self.emit(
                "recovered",
                message="Interrupted operations were recorded as uncertain; current files are retained.",
            )
        else:
            self._balance_pending(
                "Imported or interrupted tool call has no confirmed result. Inspect current state before continuing."
            )

    def _finish_turn(self, status: str) -> None:
        active = self.state.pop("active", None)
        if active is None:
            return
        active.setdefault("changes", {})
        active["end"] = len(self.state["messages"])
        active["status"] = status
        active["todos_after"] = copy.deepcopy(self.state["todos"])
        self.state["turns"].append(active)
        self.state["status"] = status
        self._save()

    def run(self, prompt: str, attachments: tuple[str, ...] = ()) -> RunResult:
        if not prompt.strip() and not attachments:
            raise CodingError("empty_prompt", "Enter a task or attach a file")
        with self._exclusive():
            self._reload()
            self._recover()
            return self._run(prompt, attachments)

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        lock_name = hashlib.sha256(str(self.settings.project).encode()).hexdigest()[:24]
        project_lock = FileLock(
            str(self.store.root.parent / (lock_name + ".lock")), timeout=0
        )
        try:
            with self.store.exclusive(self.id):
                if not self.child:
                    project_lock.acquire()
                try:
                    yield
                finally:
                    if not self.child:
                        project_lock.release()
        except Timeout as exc:
            raise CodingError(
                "project_busy", "Another agent session is running in this project"
            ) from exc

    def _run(self, prompt: str, attachments: tuple[str, ...]) -> RunResult:
        from .extensions import Extensions

        started = time.monotonic()
        deadline = threading.Timer(self.settings.max_seconds, self.stop.set)
        deadline.daemon = True
        deadline.start()
        turns = 0
        usage: Json = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        text = ""
        status = "error"
        error: str | None = None
        verification = (
            "not_configured" if not self.settings.validation_command else "not_run"
        )
        tools = ToolSet(
            self.settings,
            self.workspace,
            self.permissions,
            self.state,
            self.stop,
            question=self.question,
        )
        extensions = Extensions(self, tools)
        journal = ChangeJournal(self.workspace, self.state, self._save)
        tools.observe = journal.execute
        partial: list[str] = []
        stream: StreamingRedactor | None = None
        try:
            content = self.redactor.text(attach(self.workspace, prompt, attachments))
            self.state["active"] = {
                "start": len(self.state["messages"]),
                "changes": {},
                "todos_before": copy.deepcopy(self.state["todos"]),
                "prompt": self.redactor.text(prompt[:200]),
            }
            self.state["redo"] = []
            self.state["status"] = "running"
            self.state["mode"] = self.permissions.mode
            self.state["model"] = self.settings.provider.model
            if not self.state["messages"]:
                self.state["title"] = self.redactor.text(prompt.replace("\n", " ")[:80])
            self.state["messages"].append({"role": "user", "content": content})
            self.workspace.before_write = journal.before_write
            self._save()
            self.emit(
                "session_started",
                project=str(self.settings.project),
                mode=self.permissions.mode,
            )
            extensions.open()
            system = system_prompt(
                self.workspace, self.permissions.mode, extensions.skills.catalog()
            )
            repetitions: dict[str, int] = {}
            while True:
                if self.stop.is_set():
                    raise Cancelled()
                if (
                    turns >= self.settings.max_turns
                    or usage["total_tokens"] >= self.settings.max_tokens
                ):
                    raise CodingError(
                        "budget_exhausted",
                        "Task request or token budget exhausted; resume the session to continue",
                    )
                messages, compacted = request_messages(
                    self.redactor.text(system),
                    self.state["messages"],
                    self.settings.context_chars,
                    force=bool(self.state.pop("compact_next", False)),
                )
                if compacted:
                    self.emit(
                        "context_compacted",
                        characters=len(json.dumps(messages, ensure_ascii=False)),
                    )
                partial = []

                def relay(piece: str, partial: list[str] = partial) -> None:
                    partial.append(piece)
                    self.emit("text_delta", text=piece)

                stream = StreamingRedactor(self.redactor, relay)
                turns += 1
                completion = self.model.complete(
                    messages, tools.schemas(), on_text=stream.feed, stop=self.stop
                )
                stream.finish()
                stream = None
                # Conservative estimate when a compatible endpoint omits usage.
                for key, fallback in (
                    ("prompt_tokens", max(1, len(json.dumps(messages)) // 4)),
                    (
                        "completion_tokens",
                        max(1, len(json.dumps(completion.message())) // 4),
                    ),
                ):
                    value = completion.usage.get(key, fallback)
                    usage[key] += (
                        value if isinstance(value, int) and value >= 0 else fallback
                    )
                usage["total_tokens"] = (
                    usage["prompt_tokens"] + usage["completion_tokens"]
                )
                text = self.redactor.text(completion.text)
                message = completion.message()
                for key in ("content", "reasoning_content", "reasoning_details"):
                    if key in message:
                        message[key] = self.redactor.value(message[key])
                for raw_call in message.get("tool_calls", []):
                    raw_call["function"]["arguments"] = self.redactor.text(
                        raw_call["function"]["arguments"]
                    )
                self.state["messages"].append(message)
                partial = []
                if completion.calls and completion.finish_reason not in {
                    "tool_calls",
                    "function_call",
                    "stop",
                }:
                    raise CodingError(
                        "incomplete_response",
                        "Model output ended before complete tool arguments; no tools were executed",
                    )
                # Journal all calls before the first tool can have side effects.
                self.state["pending"] = [call.id for call in completion.calls]
                self._save()
                self.emit(
                    "response_finished",
                    usage=completion.usage,
                    finish_reason=completion.finish_reason,
                )
                if not completion.calls:
                    if completion.finish_reason not in {"stop", "end_turn"}:
                        raise CodingError(
                            "incomplete_response",
                            "Model output was truncated or filtered; resume to continue",
                        )
                    changed = bool(self.state["active"]["changes"])
                    if (
                        changed
                        and self.settings.validation_command
                        and self.permissions.mode == "build"
                    ):
                        if tools.validated_snapshot != self.workspace.capture():
                            self.emit(
                                "validation_started",
                                command=list(self.settings.validation_command),
                            )
                            validation = tools.run_validation()
                            self.emit("validation_finished", result=validation)
                            if not validation["passed"]:
                                self.state["messages"].append(
                                    {
                                        "role": "user",
                                        "content": "Host validation failed. Repair the issue and rerun test:\n"
                                        + json.dumps(self.redactor.value(validation)),
                                    }
                                )
                                self._save()
                                verification = "failed"
                                continue
                        verification = "passed"
                    elif tools.validation:
                        verification = (
                            "passed"
                            if tools.validated_snapshot == self.workspace.capture()
                            else "failed"
                        )
                    status = "completed"
                    break
                for call in completion.calls:
                    if self.stop.is_set():
                        raise Cancelled()
                    fingerprint = call.name + ":" + call.arguments
                    repetitions[fingerprint] = repetitions.get(fingerprint, 0) + 1
                    if repetitions[fingerprint] > 5:
                        raise CodingError(
                            "no_progress",
                            "The same tool operation repeated six times; inspect results before continuing",
                        )
                    self.emit(
                        "tool_started",
                        id=call.id,
                        tool=call.name,
                        arguments=self.redactor.text(call.arguments),
                    )
                    fatal: CodingError | None = None
                    try:
                        self.remaining_turns = self.settings.max_turns - turns
                        self.remaining_tokens = (
                            self.settings.max_tokens - usage["total_tokens"]
                        )
                        outcome = tools.execute(call.name, call.arguments)
                        if call.name == "task":
                            turns += outcome.get("turns", 0)
                            for key in usage:
                                usage[key] += outcome.get("usage", {}).get(key, 0)
                        if outcome.get("changed"):
                            repetitions.clear()
                        if outcome.get("cancelled"):
                            fatal = Cancelled()
                    except CodingError as exc:
                        outcome = {
                            "error": exc.code,
                            "message": str(exc),
                            **exc.details,
                        }
                        if exc.code in {"permission_required", "cancelled"}:
                            fatal = exc
                    outcome = self.redactor.value(outcome)
                    self.state["messages"].append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(outcome, ensure_ascii=False),
                        }
                    )
                    self.state["pending"].remove(call.id)
                    self._save()
                    self.emit(
                        "tool_finished", id=call.id, tool=call.name, result=outcome
                    )
                    if fatal:
                        raise fatal
        except (CodingError, KeyboardInterrupt) as exc:
            if stream:
                stream.finish()
            if partial:
                text = "".join(partial)
                self.state["messages"].append({"role": "assistant", "content": text})
            code = exc.code if isinstance(exc, CodingError) else "cancelled"
            if (
                self.stop.is_set()
                and time.monotonic() - started >= self.settings.max_seconds
            ):
                code = "budget_exhausted"
            status = (
                code
                if code in {"permission_required", "budget_exhausted", "cancelled"}
                else "error"
            )
            error = self.redactor.text(str(exc) or "Operation cancelled")
            self._balance_pending(
                "Operation was interrupted and has no confirmed result. Inspect current files before retrying."
            )
            self.emit("error", code=code, message=error)
        finally:
            deadline.cancel()
            extensions.close()
            self.workspace.before_write = None
            self.state["usage"] = {
                key: self.state.get("usage", {}).get(key, 0) + value
                for key, value in usage.items()
            }
            self._finish_turn(status)
        result = RunResult(self.id, status, text, turns, usage, verification, error)
        self.emit("result", **asdict(result))
        return result

    def explore(self, prompt: str) -> Json:
        if self.remaining_turns <= 0 or self.remaining_tokens <= 0:
            raise CodingError(
                "budget_exhausted", "No parent budget remains for exploration"
            )
        settings = replace(
            self.settings,
            mode="plan",
            max_turns=min(8, self.remaining_turns),
            max_tokens=min(40_000, self.remaining_tokens),
            mcp={},
        )
        child = CodingSession(settings, model=self.model, stop=self.stop, child=True)
        child.permissions.rules = [
            *self.permissions.rules,
            {"tool": "task", "action": "deny"},
            {"tool": "question", "action": "deny"},
        ]
        result = child.run(prompt)
        return {
            "session_id": child.id,
            "status": result.status,
            "text": result.text,
            "turns": result.turns,
            "usage": result.usage,
            "error": result.error,
        }

    def diff(self) -> str:
        self._reload()
        return (
            self.workspace.diff(self.state["turns"][-1].get("changes", {}))
            if self.state["turns"]
            else ""
        )

    def undo(self, *, redo: bool = False) -> str:
        with self._exclusive():
            self._reload()
            if self.state.get("active"):
                self._recover()
            if redo:
                stack = self.state.get("redo") or []
                if not stack:
                    raise CodingError("nothing_to_redo", "No undone turn to restore")
                turn = stack[-1]
                if turn.get("uncertain"):
                    raise CodingError(
                        "uncertain_changes",
                        "Interrupted operation has uncertain file ownership; inspect and restore those files manually",
                    )
                self.workspace.restore(turn["changes"], undo=False)
                self.state["messages"].extend(turn.pop("removed_messages"))
                self.state["todos"] = turn["todos_after"]
                self.state["turns"].append(stack.pop())
            else:
                if not self.state["turns"]:
                    raise CodingError("nothing_to_undo", "No turn to undo")
                turn = self.state["turns"][-1]
                if turn.get("uncertain"):
                    raise CodingError(
                        "uncertain_changes",
                        "Interrupted operation has uncertain file ownership; inspect and restore those files manually",
                    )
                self.workspace.restore(turn["changes"], undo=True)
                turn["removed_messages"] = self.state["messages"][turn["start"] :]
                self.state["messages"] = self.state["messages"][: turn["start"]]
                self.state["todos"] = turn["todos_before"]
                self.state["redo"] = [
                    *(self.state.get("redo") or []),
                    self.state["turns"].pop(),
                ]
            self.state["status"] = "idle"
            self._save()
            self.emit("redo" if redo else "undo", paths=sorted(turn["changes"]))
            return self.workspace.diff(turn["changes"])

    def compact(self) -> None:
        with self.store.exclusive(self.id):
            self._reload()
            self.state["compact_next"] = True
            self._save()
