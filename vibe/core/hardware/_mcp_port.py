from __future__ import annotations

from typing import Any, Protocol


class MCPCallResult(Protocol):
    structured: dict[str, Any] | None
    text: str | None


class MCPCallPort(Protocol):
    async def call_tool(
        self,
        *,
        command: list[str],
        tool_name: str,
        arguments: dict[str, Any],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        startup_timeout_sec: float | None = None,
        tool_timeout_sec: float | None = None,
        sampling_callback: Any = None,
    ) -> MCPCallResult: ...

    async def call_interrupt_tool(
        self,
        *,
        command: list[str],
        tool_name: str,
        arguments: dict[str, Any],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        startup_timeout_sec: float | None = None,
        tool_timeout_sec: float | None = None,
        sampling_callback: Any = None,
    ) -> MCPCallResult: ...


__all__ = ["MCPCallPort", "MCPCallResult"]
