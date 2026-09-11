"""Stable application errors shared by the Harness and UI."""

from __future__ import annotations

from typing import Any, Dict, Optional


class AgentHarnessError(Exception):
    """An error with a stable code and a user-facing message."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
            "details": self.details,
        }


class ConfigError(AgentHarnessError):
    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            "invalid_config",
            message,
            recoverable=True,
            details=details,
        )


class WorkspaceError(AgentHarnessError):
    pass


class ToolExecutionError(AgentHarnessError):
    pass


class ModelAPIError(AgentHarnessError):
    pass


class TraceWriteError(AgentHarnessError):
    def __init__(self, message: str) -> None:
        super().__init__("trace_write_failed", message, recoverable=True)

