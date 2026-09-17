from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import pytest

from vibe.core.tools.mcp.pool import MCPConnectionPool


@pytest.mark.asyncio
async def test_interrupt_call_bypasses_an_inflight_serialized_call() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def call_tool(
        tool_name: str,
        arguments: dict[str, object],
        read_timeout_seconds: object = None,
    ) -> SimpleNamespace:
        _ = arguments, read_timeout_seconds
        order.append(f"{tool_name}:start")
        if tool_name == "move":
            started.set()
            await release.wait()
        else:
            release.set()
        order.append(f"{tool_name}:end")
        return SimpleNamespace(structuredContent={"ok": 1}, content=None)

    session = SimpleNamespace(call_tool=AsyncMock(side_effect=call_tool))

    async def enter_session(*args: object, **kwargs: object) -> SimpleNamespace:
        _ = args, kwargs
        return session

    pool = MCPConnectionPool()
    with patch(
        "vibe.core.tools.mcp.pool.enter_stdio_session", side_effect=enter_session
    ):
        move = asyncio.create_task(
            pool.call_tool(command=["srv"], tool_name="move", arguments={})
        )
        await started.wait()
        await pool.call_interrupt_tool(command=["srv"], tool_name="stop", arguments={})
        await move
        await pool.aclose()

    assert order == ["move:start", "stop:start", "stop:end", "move:end"]


@pytest.mark.asyncio
async def test_transport_reconnect_cancels_inflight_interrupt() -> None:
    move_started = asyncio.Event()
    interrupt_started = asyncio.Event()
    fail_move = asyncio.Event()

    async def dead_call(
        tool_name: str,
        arguments: dict[str, object],
        read_timeout_seconds: object = None,
    ) -> SimpleNamespace:
        _ = arguments, read_timeout_seconds
        if tool_name == "move":
            move_started.set()
            await fail_move.wait()
            raise anyio.ClosedResourceError
        interrupt_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    dead_session = SimpleNamespace(call_tool=AsyncMock(side_effect=dead_call))
    live_session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(structuredContent={"ok": 1}, content=None)
        )
    )
    sessions = iter([dead_session, live_session])

    async def enter_session(*args: object, **kwargs: object) -> SimpleNamespace:
        _ = args, kwargs
        return next(sessions)

    pool = MCPConnectionPool()
    with patch(
        "vibe.core.tools.mcp.pool.enter_stdio_session", side_effect=enter_session
    ):
        move = asyncio.create_task(
            pool.call_tool(command=["srv"], tool_name="move", arguments={})
        )
        await move_started.wait()
        stop = asyncio.create_task(
            pool.call_interrupt_tool(command=["srv"], tool_name="stop", arguments={})
        )
        await interrupt_started.wait()
        fail_move.set()

        assert (await move).structured == {"ok": 1}
        with pytest.raises(asyncio.CancelledError):
            await stop
        await pool.aclose()
