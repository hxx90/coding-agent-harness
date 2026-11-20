from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable
from contextlib import suppress

from vibe.core.hardware import HardwareRuntime
from vibe.core.types import ToolStreamEvent


async def stream_policy_progress[T](
    operation: Awaitable[T], *, runtime: HardwareRuntime, run_id: str, tool_call_id: str
) -> AsyncGenerator[ToolStreamEvent | T, None]:
    """Observe existing telemetry; never execute or retry a hardware operation.

    The owned task is always joined, including when the consumer closes the
    stream. Cancellation still passes through the policy runner's stop path.
    Only compact event labels are streamed, never point clouds or tool results.
    """
    task = asyncio.ensure_future(operation)
    try:
        events = runtime.read_trace(run_id).events
        cursor = events[-1].sequence if events else 0
        while True:
            await asyncio.wait({task}, timeout=0.1)
            for event in runtime.read_trace(run_id).events:
                if event.sequence <= cursor:
                    continue
                cursor = event.sequence
                detail = event.payload.get("action") or event.payload.get("status")
                message = f"#{event.sequence} {event.kind}"
                if isinstance(detail, str):
                    message += f" · {detail}"
                yield ToolStreamEvent(
                    tool_name="robo_run_policy",
                    tool_call_id=tool_call_id,
                    message=message + "\n",
                )
            if task.done():
                yield task.result()
                break
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
