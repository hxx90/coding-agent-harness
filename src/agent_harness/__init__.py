"""Coding Agent Harness: terminal agent and optional legacy web application."""

from .domain import ExecutionMode, TaskState
from .orchestrator import TaskOrchestrator

__all__ = ["ExecutionMode", "TaskOrchestrator", "TaskState"]
__version__ = "2.0.0"
