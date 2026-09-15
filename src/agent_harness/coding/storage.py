"""Atomic durable sessions with an OS-backed lock for each active session."""

from __future__ import annotations

import base64
import json
import math
import os
import re
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from agent_harness.trace import Redactor

from .media import MediaStore, validate_content
from .types import CodingError, Json

MAX_SESSION_BYTES = 512 * 1024 * 1024


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class SessionStore:
    def __init__(self, data_dir: Path):
        self.media = MediaStore(data_dir / "media")
        self.root = data_dir / "cli" / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def create(self, project: Path, model: str, mode: str = "build") -> Json:
        state: Json = {
            "schema_version": 2,
            "id": uuid.uuid4().hex,
            "project": str(project.resolve()),
            "model": model,
            "mode": mode,
            "title": "New session",
            "created": time.time(),
            "updated": time.time(),
            "status": "idle",
            "messages": [],
            "turns": [],
            "todos": [],
            "usage": {},
            "redo": None,
        }
        self.save(state)
        return state

    def resolve(self, value: str, project: Path | None = None) -> str:
        if value == "latest":
            sessions = self.list(project)
            if not sessions:
                raise CodingError(
                    "session_not_found", "No previous session for this project"
                )
            return str(sessions[0]["id"])
        if not re.fullmatch(r"[a-f0-9]{4,32}", value):
            raise CodingError(
                "session_not_found",
                "Session ID must be at least four hexadecimal characters",
            )
        matches = [
            p.stem for p in self.root.glob(value + "*.json") if not p.is_symlink()
        ]
        if not matches:
            raise CodingError("session_not_found", f"Session not found: {value}")
        if len(matches) != 1:
            raise CodingError(
                "ambiguous_session", f"Session prefix is ambiguous: {value}"
            )
        return matches[0]

    def _path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise CodingError("invalid_session", "Invalid session ID")
        path = self.root / (session_id + ".json")
        if path.is_symlink():
            raise CodingError(
                "invalid_session", "Session file cannot be a symbolic link"
            )
        return path

    def load(self, session_id: str) -> Json:
        path = self._path(session_id)
        try:
            if path.stat().st_size > MAX_SESSION_BYTES:
                raise ValueError("session exceeds size limit")
            state = json.loads(path.read_text(encoding="utf-8"))
            self._validate(state)
            if state["id"] != session_id:
                raise ValueError("session ID does not match filename")
            return state
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise CodingError(
                "invalid_session", f"Cannot load session {session_id}: {exc}"
            ) from exc

    @staticmethod
    def _validate(state: Any, *, history: bool = True) -> None:
        if not isinstance(state, dict) or state.get("schema_version") not in {1, 2}:
            raise ValueError("unsupported session format")
        for key in ("id", "project", "model", "mode", "title", "status"):
            if not isinstance(state.get(key), str):
                raise TypeError(f"invalid {key}")
        for key in ("messages", "turns", "todos"):
            if not isinstance(state.get(key), list):
                raise TypeError(f"invalid {key}")
        for message in state["messages"]:
            if not isinstance(message, dict) or message.get("role") not in {
                "user",
                "assistant",
                "tool",
                "system",
            }:
                raise ValueError("invalid message")
        if state["mode"] not in {"plan", "build"}:
            raise ValueError("invalid mode")
        for key in ("created", "updated"):
            if not isinstance(state.get(key), (int, float)) or not math.isfinite(
                state[key]
            ):
                raise ValueError(f"invalid {key}")
        if not isinstance(state.get("usage"), dict):
            raise TypeError("invalid usage")
        pending: set[str] = set()
        for message in state["messages"]:
            validate_content(message.get("content"))
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list):
                raise TypeError("invalid tool calls")
            if calls and message["role"] != "assistant":
                raise ValueError("tool calls require an assistant message")
            if message["role"] != "tool" and pending:
                raise ValueError("unbalanced tool conversation")
            for call in calls:
                if (
                    not isinstance(call, dict)
                    or not isinstance(call.get("id"), str)
                    or call["id"] in pending
                ):
                    raise ValueError("invalid or duplicate tool ID")
                function = call.get("function")
                if not isinstance(function, dict) or not all(
                    isinstance(function.get(key), str) for key in ("name", "arguments")
                ):
                    raise ValueError("invalid tool function")
                pending.add(call["id"])
            if message["role"] == "tool":
                if message.get("tool_call_id") not in pending:
                    raise ValueError("orphan tool result")
                pending.remove(message["tool_call_id"])
        if history:
            SessionStore._validate_history(state)

    @staticmethod
    def _validate_history(state: Json) -> None:
        def snapshot(value: Any) -> None:
            if value is None:
                return
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("data"), str)
                or not isinstance(value.get("mode"), int)
            ):
                raise TypeError("invalid file snapshot")
            if not 0 <= value["mode"] <= 0o777:
                raise ValueError("invalid snapshot mode")
            try:
                base64.b64decode(value["data"], validate=True)
            except ValueError as exc:
                raise ValueError("invalid snapshot encoding") from exc

        redo = state.get("redo") or []
        if not isinstance(redo, list):
            raise TypeError("invalid redo history")
        for turn in [*state["turns"], *redo]:
            if (
                not isinstance(turn, dict)
                or not isinstance(turn.get("start"), int)
                or turn["start"] < 0
                or not isinstance(turn.get("changes"), dict)
            ):
                raise TypeError("invalid turn history")
            if not isinstance(turn.get("todos_before"), list) or not isinstance(
                turn.get("todos_after"), list
            ):
                raise TypeError("invalid turn checklist")
            for name, versions in turn["changes"].items():
                if (
                    not isinstance(name, str)
                    or not isinstance(versions, dict)
                    or not {"before", "after"} <= versions.keys()
                ):
                    raise ValueError("invalid turn changes")
                snapshot(versions["before"])
                snapshot(versions["after"])
        if any(not isinstance(turn.get("removed_messages"), list) for turn in redo):
            raise ValueError("invalid redo messages")
        active = state.get("active")
        if active is not None:
            if (
                not isinstance(active, dict)
                or not isinstance(active.get("start"), int)
                or not isinstance(active.get("todos_before"), list)
            ):
                raise ValueError("invalid active turn")
            effect = active.get("effect")
            snapshots = active.get("before", {})
            if effect is not None:
                if not isinstance(effect, dict) or not isinstance(
                    effect.get("tool"), str
                ):
                    raise ValueError("invalid active effect")
                snapshots = effect.get("before")
            if not isinstance(snapshots, dict):
                raise ValueError("invalid active snapshot")
            for value in snapshots.values():
                snapshot(value)
        if not isinstance(state.get("extra_paths", []), list) or any(
            not isinstance(path, str) for path in state.get("extra_paths", [])
        ):
            raise ValueError("invalid extra paths")

    def save(self, state: Json) -> None:
        self._validate(state)
        state["updated"] = time.time()
        if (
            len(json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))
            > MAX_SESSION_BYTES
        ):
            raise CodingError(
                "session_too_large",
                "Session exceeds 512 MiB; start a new session. The last durable state is retained.",
            )
        atomic_json(self._path(state["id"]), state)

    @contextmanager
    def exclusive(self, session_id: str) -> Iterator[None]:
        path = self._path(session_id)
        lock = FileLock(str(path.with_suffix(".lock")), timeout=0)
        try:
            lock.acquire()
        except Timeout as exc:
            raise CodingError(
                "session_busy", "This session is running in another process"
            ) from exc
        try:
            yield
        finally:
            lock.release()

    def list(self, project: Path | None = None) -> list[Json]:
        result = []
        for path in self.root.glob("*.json"):
            try:
                state = self.load(path.stem)
                if project is not None and state["project"] != str(project.resolve()):
                    continue
                result.append(
                    {
                        key: state[key]
                        for key in (
                            "id",
                            "project",
                            "model",
                            "title",
                            "status",
                            "created",
                            "updated",
                        )
                    }
                )
            except CodingError:
                # A damaged session must not hide other recoverable sessions.
                continue
        return sorted(result, key=lambda item: item["updated"], reverse=True)

    def export(self, session_id: str, destination: Path) -> None:
        if destination.exists():
            raise CodingError(
                "file_exists", f"Export destination already exists: {destination}"
            )
        state = self.load(session_id)
        state["turns"] = []
        state["redo"] = None
        state.pop("active", None)
        state.pop("pending", None)
        for message in state["messages"]:
            message.pop("reasoning_content", None)
            message.pop("reasoning_details", None)
        exported = Redactor().value(state)
        # Content IDs and binary encodings are not prose: preserve them exactly.
        for original, copied in zip(state["messages"], exported["messages"]):
            if isinstance(original.get("content"), list):
                for part, target in zip(original["content"], copied["content"]):
                    if part["type"] == "image_ref":
                        target.update(part)
        exported["media_blobs"] = self.media.export(state["messages"])
        atomic_json(destination, exported)

    def import_session(self, source: Path, project: Path) -> Json:
        try:
            if source.stat().st_size > MAX_SESSION_BYTES:
                raise ValueError("session exceeds size limit")
            state = json.loads(source.read_text(encoding="utf-8"))
            self._validate(state, history=False)
            self.media.restore(state["messages"], state.pop("media_blobs", {}))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise CodingError(
                "invalid_session", f"Cannot import session: {exc}"
            ) from exc
        state["id"] = uuid.uuid4().hex
        state["project"] = str(project.resolve())
        state["status"] = "idle"
        state["created"] = time.time()
        # Imported conversations provide context, never authority to overwrite files.
        state["turns"] = []
        state["redo"] = None
        state.pop("pending", None)
        state.pop("active", None)
        state.pop("extra_paths", None)
        self.save(state)
        return state

    def delete(self, session_id: str) -> None:
        with self.exclusive(session_id):
            self._path(session_id).unlink()
            events = self.root / (session_id + ".jsonl")
            events.unlink(missing_ok=True)

    def append_event(self, session_id: str, payload: Json) -> None:
        path = self._path(session_id).with_suffix(".jsonl")
        with path.open("a", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
