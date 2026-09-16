from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
import subprocess
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, JsonValue

from vibe.core.hardware import (
    CommandReceipt,
    DeviceCapabilityManifest,
    DeviceLease,
    DeviceSnapshot,
    HardwareCommand,
    HardwareRuntime,
    HardwareRuntimeError,
    RunTrace,
    StopReceipt,
)
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    InvokeContext,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.models import PermissionContext
from vibe.core.types import ToolStreamEvent


class RoboDevicesArgs(BaseModel):
    action: Literal[
        "list", "describe", "connect", "acquire", "arm", "disarm", "release"
    ]
    device_id: str | None = None
    owner: str = "robo-agent"
    lease_id: str | None = None
    ttl_seconds: float = Field(default=300.0, gt=0, le=3600)
    run_id: str | None = None


class RoboDevicesResult(BaseModel):
    action: str
    devices: list[DeviceCapabilityManifest] = Field(default_factory=list)
    snapshot: DeviceSnapshot | None = None
    lease: DeviceLease | None = None
    released: bool = False


class RoboDevicesConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ALWAYS


class RoboDevices(
    BaseTool[RoboDevicesArgs, RoboDevicesResult, RoboDevicesConfig, BaseToolState]
):
    def resolve_permission(self, args: RoboDevicesArgs) -> PermissionContext | None:
        if args.action == "arm":
            return PermissionContext(
                permission=ToolPermission.ASK, reason="Arming enables device motion"
            )
        return None

    async def run(
        self, args: RoboDevicesArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | RoboDevicesResult, None]:
        runtime = _runtime(ctx)
        run_id = _run_id(args.run_id, ctx)
        try:
            match args.action:
                case "list":
                    yield RoboDevicesResult(
                        action="list", devices=await runtime.discover()
                    )
                case "describe":
                    device_id = _required(args.device_id, "device_id")
                    manifests = await runtime.discover()
                    selected = [m for m in manifests if m.device_id == device_id]
                    if not selected:
                        raise ToolError(f"Unknown Robo device: {device_id}")
                    yield RoboDevicesResult(action="describe", devices=selected)
                case "connect":
                    device_id = _required(args.device_id, "device_id")
                    yield RoboDevicesResult(
                        action="connect",
                        snapshot=await runtime.connect(device_id, run_id=run_id),
                    )
                case "acquire":
                    device_id = _required(args.device_id, "device_id")
                    yield RoboDevicesResult(
                        action="acquire",
                        lease=await runtime.acquire_lease(
                            device_id,
                            owner=args.owner,
                            ttl_seconds=args.ttl_seconds,
                            run_id=run_id,
                        ),
                    )
                case "arm":
                    device_id = _required(args.device_id, "device_id")
                    lease_id = _required(args.lease_id, "lease_id")
                    yield RoboDevicesResult(
                        action="arm",
                        snapshot=await runtime.arm(
                            device_id, lease_id=lease_id, run_id=run_id
                        ),
                    )
                case "disarm":
                    device_id = _required(args.device_id, "device_id")
                    yield RoboDevicesResult(
                        action="disarm",
                        snapshot=await runtime.disarm(device_id, run_id=run_id),
                    )
                case "release":
                    device_id = _required(args.device_id, "device_id")
                    lease_id = _required(args.lease_id, "lease_id")
                    await runtime.release_lease(
                        device_id, lease_id=lease_id, run_id=run_id
                    )
                    yield RoboDevicesResult(action="release", released=True)
        except HardwareRuntimeError as exc:
            raise ToolError(str(exc), model_detail=f"error_code={exc.code}") from exc


class RoboExecuteArgs(BaseModel):
    device_id: str
    lease_id: str
    action: str
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    command_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str | None = None


class RoboExecuteResult(BaseModel):
    receipt: CommandReceipt


class RoboExecuteConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK


class RoboExecute(
    BaseTool[RoboExecuteArgs, RoboExecuteResult, RoboExecuteConfig, BaseToolState]
):
    async def run(
        self, args: RoboExecuteArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | RoboExecuteResult, None]:
        runtime = _runtime(ctx)
        command = HardwareCommand.model_validate(
            args.model_dump(exclude={"run_id"}, mode="json")
        )
        try:
            receipt = await runtime.execute(command, run_id=_run_id(args.run_id, ctx))
        except HardwareRuntimeError as exc:
            raise ToolError(str(exc), model_detail=f"error_code={exc.code}") from exc
        yield RoboExecuteResult(receipt=receipt)


class RoboSequenceAction(BaseModel):
    action: str
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    command_id: str = Field(default_factory=lambda: uuid4().hex)


class RoboSequenceArgs(BaseModel):
    device_id: str
    lease_id: str
    actions: list[RoboSequenceAction] = Field(min_length=1, max_length=50)
    run_id: str | None = None


class RoboSequenceResult(BaseModel):
    receipts: list[CommandReceipt]


class RoboSequenceConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK


class RoboSequence(
    BaseTool[RoboSequenceArgs, RoboSequenceResult, RoboSequenceConfig, BaseToolState]
):
    async def run(
        self, args: RoboSequenceArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | RoboSequenceResult, None]:
        runtime = _runtime(ctx)
        commands = [
            HardwareCommand(
                command_id=action.command_id,
                device_id=args.device_id,
                lease_id=args.lease_id,
                action=action.action,
                parameters=action.parameters,
            )
            for action in args.actions
        ]
        try:
            receipts = await runtime.execute_sequence(
                commands, run_id=_run_id(args.run_id, ctx)
            )
        except HardwareRuntimeError as exc:
            raise ToolError(str(exc), model_detail=f"error_code={exc.code}") from exc
        yield RoboSequenceResult(receipts=receipts)


class WorkspaceObservation(BaseModel):
    cwd: str
    git_commit: str | None = None
    git_branch: str | None = None
    dirty: bool = False
    changed_files: list[str] = Field(default_factory=list)


class RoboObserveArgs(BaseModel):
    device_id: str | None = None
    run_id: str | None = None
    include_workspace: bool = True


class RoboObserveResult(BaseModel):
    snapshot: DeviceSnapshot | None = None
    trace: RunTrace
    workspace: WorkspaceObservation | None = None


class RoboObserveConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ALWAYS
    max_changed_files: int = Field(default=100, ge=1, le=1000)


class RoboObserve(
    BaseTool[RoboObserveArgs, RoboObserveResult, RoboObserveConfig, BaseToolState]
):
    async def run(
        self, args: RoboObserveArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | RoboObserveResult, None]:
        runtime = _runtime(ctx)
        run_id = _run_id(args.run_id, ctx)
        try:
            snapshot = (
                await runtime.observe(args.device_id, run_id=run_id)
                if args.device_id
                else None
            )
            trace = runtime.read_trace(run_id)
        except HardwareRuntimeError as exc:
            raise ToolError(str(exc), model_detail=f"error_code={exc.code}") from exc
        workspace = (
            await asyncio.to_thread(
                _observe_workspace, self.cwd, self.config.max_changed_files
            )
            if args.include_workspace
            else None
        )
        yield RoboObserveResult(snapshot=snapshot, trace=trace, workspace=workspace)


class RoboStopArgs(BaseModel):
    device_id: str
    reason: str
    run_id: str | None = None


class RoboStopResult(BaseModel):
    receipt: StopReceipt


class RoboStopConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ALWAYS


class RoboStop(BaseTool[RoboStopArgs, RoboStopResult, RoboStopConfig, BaseToolState]):
    async def run(
        self, args: RoboStopArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | RoboStopResult, None]:
        runtime = _runtime(ctx)
        try:
            receipt = await runtime.stop(
                args.device_id, reason=args.reason, run_id=_run_id(args.run_id, ctx)
            )
        except HardwareRuntimeError as exc:
            raise ToolError(str(exc), model_detail=f"error_code={exc.code}") from exc
        yield RoboStopResult(receipt=receipt)


def _runtime(ctx: InvokeContext | None) -> HardwareRuntime:
    if ctx is None or ctx.hardware_runtime is None:
        raise ToolError("Robo hardware runtime is unavailable in this session")
    return ctx.hardware_runtime


def _run_id(requested: str | None, ctx: InvokeContext | None) -> str:
    if requested:
        return requested
    if ctx is not None and ctx.session_id:
        return ctx.session_id
    if ctx is not None:
        return ctx.tool_call_id
    return uuid4().hex


def _required(value: str | None, name: str) -> str:
    if value:
        return value
    raise ToolError(f"{name} is required for this action")


def _observe_workspace(cwd: Path, max_files: int) -> WorkspaceObservation:
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return WorkspaceObservation(cwd=str(cwd))
    if result.returncode != 0:
        return WorkspaceObservation(cwd=str(cwd))
    lines = result.stdout.splitlines()
    branch = lines[0].removeprefix("## ").split("...")[0] if lines else None
    changed = lines[1 : max_files + 1]
    return WorkspaceObservation(
        cwd=str(cwd),
        git_commit=commit.stdout.strip() if commit.returncode == 0 else None,
        git_branch=branch,
        dirty=bool(changed),
        changed_files=changed,
    )


__all__ = [
    "RoboDevices",
    "RoboDevicesArgs",
    "RoboDevicesConfig",
    "RoboDevicesResult",
    "RoboExecute",
    "RoboExecuteArgs",
    "RoboExecuteConfig",
    "RoboExecuteResult",
    "RoboObserve",
    "RoboObserveArgs",
    "RoboObserveConfig",
    "RoboObserveResult",
    "RoboSequence",
    "RoboSequenceAction",
    "RoboSequenceArgs",
    "RoboSequenceConfig",
    "RoboSequenceResult",
    "RoboStop",
    "RoboStopArgs",
    "RoboStopConfig",
    "RoboStopResult",
    "WorkspaceObservation",
]
