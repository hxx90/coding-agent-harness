"""Core domain types for the local Coding Agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ExecutionMode(str, Enum):
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class TaskState(str, Enum):
    IDLE = "idle"
    ANALYZING = "analyzing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    SUCCESS = "success"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"
    REJECTED = "rejected"
    REVERTED = "reverted"


ACTIVE_STATES = {
    TaskState.ANALYZING,
    TaskState.EXECUTING,
    TaskState.VALIDATING,
    TaskState.REPAIRING,
}

RESUMABLE_STATES = {
    TaskState.CANCELLED,
    TaskState.BUDGET_EXHAUSTED,
    TaskState.FAILED,
}


@dataclass(frozen=True)
class NormalizedToolCall:
    call_id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ModelResponse:
    text: Optional[str] = None
    tool_calls: List[NormalizedToolCall] = field(default_factory=list)
    stop_reason: str = "stop"
    usage: Optional[Dict[str, Any]] = None
    raw_response_id: Optional[str] = None
    provider_message_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    recoverable: bool = True

    @classmethod
    def success(cls, **data: Any) -> "ToolResult":
        return cls(ok=True, data=data)

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        recoverable: bool = True,
        **details: Any,
    ) -> "ToolResult":
        payload = dict(details)
        return cls(
            ok=False,
            data=payload,
            error_code=code,
            error_message=message,
            recoverable=recoverable,
        )

    def to_payload(self) -> Dict[str, Any]:
        if self.ok:
            return {"ok": True, **self.data}
        return {
            "ok": False,
            "error": {
                "code": self.error_code,
                "message": self.error_message,
                "recoverable": self.recoverable,
                "details": self.data,
            },
        }


@dataclass
class Plan:
    task_summary: str
    relevant_files: List[str]
    planned_changes: List[str]
    expected_modified_files: List[str]
    validation_plan: str
    risks_or_questions: List[str] = field(default_factory=list)

    @classmethod
    def from_arguments(cls, arguments: Dict[str, Any]) -> "Plan":
        required_strings = ("task_summary", "validation_plan")
        for key in required_strings:
            value = arguments.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("%s must be a non-empty string" % key)

        required_lists = (
            "relevant_files",
            "planned_changes",
            "expected_modified_files",
        )
        normalized: Dict[str, List[str]] = {}
        for key in required_lists:
            value = arguments.get(key)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise ValueError("%s must be a list of non-empty strings" % key)
            normalized[key] = [item.strip() for item in value]

        risks = arguments.get("risks_or_questions", [])
        if not isinstance(risks, list) or not all(isinstance(item, str) for item in risks):
            raise ValueError("risks_or_questions must be a list of strings")

        return cls(
            task_summary=arguments["task_summary"].strip(),
            relevant_files=normalized["relevant_files"],
            planned_changes=normalized["planned_changes"],
            expected_modified_files=normalized["expected_modified_files"],
            validation_plan=arguments["validation_plan"].strip(),
            risks_or_questions=[item.strip() for item in risks if item.strip()],
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    exit_code: Optional[int]
    timed_out: bool
    cancelled: bool
    duration_ms: int
    stdout: str
    stderr: str
    truncated: bool = False
    runner_error: Optional[str] = None

    @property
    def passed(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.cancelled
            and self.runner_error is None
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RevertResult:
    ok: bool
    restored_files: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)
    conflicted_files: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
