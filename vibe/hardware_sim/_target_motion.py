from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import time

from vibe.hardware_sim.models import (
    MotionReason,
    TargetMoveParameters,
    TargetMoveReport,
)

Position = tuple[float, float, float]


async def run_target_motion(
    request: TargetMoveParameters,
    *,
    position: Callable[[], Position],
    advance: Callable[[Position], Awaitable[None]],
    cycles_used: Callable[[], int],
    cycles_remaining: Callable[[], int | None],
    stopped: Callable[[], bool],
    finish: Callable[[TargetMoveReport], None],
    trace_uri: str | None = None,
) -> TargetMoveReport:
    """Feedback between control periods; never swallow caller cancellation."""
    start = time.monotonic()
    first_cycle = cycles_used()
    settled = 0
    reason: MotionReason = "error"
    try:
        while cycles_used() - first_cycle < request.max_steps:
            if stopped():
                raise asyncio.CancelledError
            if time.monotonic() - start >= request.timeout_seconds:
                reason = "time_limit"
                break
            remaining = cycles_remaining()
            if remaining is not None and remaining <= 0:
                reason = "cycle_budget"
                break
            await advance(_error(request.target_xyz, position()))
            if time.monotonic() - start >= request.timeout_seconds:
                reason = "time_limit"
                break
            error = _error(request.target_xyz, position())
            within = all(
                abs(e) < tolerance
                for e, tolerance in zip(error, request.tolerance_xyz, strict=True)
            )
            settled = settled + 1 if within else 0
            if settled >= request.settle_steps:
                reason = "reached"
                break
        else:
            reason = "step_limit"
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    finally:
        current = position()
        error = _error(request.target_xyz, current)
        report = TargetMoveReport(
            reached=reason == "reached",
            reason=reason,
            target_xyz=request.target_xyz,
            position_xyz=current,
            error_xyz=error,
            error_max_m=max(map(abs, error)),
            cycles=cycles_used() - first_cycle,
            settled_cycles=settled,
            elapsed_seconds=time.monotonic() - start,
            trace_uri=trace_uri,
        )
        finish(report)
    return report


def _error(target: Position, current: Position) -> Position:
    return (target[0] - current[0], target[1] - current[1], target[2] - current[2])
