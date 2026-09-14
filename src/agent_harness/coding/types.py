"""Public outcomes and errors shared by the CLI and the session engine."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Json = dict[str, Any]
EventSink = Callable[[Json], None]


class CodingError(Exception):
    """An actionable error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, details: Json | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class Cancelled(CodingError):
    def __init__(self, message: str = "Operation cancelled") -> None:
        super().__init__("cancelled", message)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class Completion:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: Json = field(default_factory=dict)
    finish_reason: str = "stop"
    extra: Json = field(default_factory=dict)

    def message(self) -> Json:
        message: Json = {
            "role": "assistant",
            "content": self.text or None,
            **self.extra,
        }
        if self.calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.calls
            ]
        return message


@dataclass
class RunResult:
    session_id: str
    status: str
    text: str
    turns: int
    usage: Json
    verification: str = "not_run"
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return {
            "completed": 0,
            "error": 1,
            "permission_required": 3,
            "budget_exhausted": 4,
            "cancelled": 130,
        }.get(self.status, 1)


def event(kind: str, **data: Any) -> Json:
    return {"type": kind, "timestamp": time.time(), **data}
