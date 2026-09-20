from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
import subprocess
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, Field, JsonValue

from vibe.core.config import VibeConfigSchema
from vibe.core.hardware import (
    CommandReceipt,
    DeviceCapabilityManifest,
    DeviceLease,
    DeviceSnapshot,
    HardwareCommand,
    HardwarePolicyError,
    HardwarePolicyResult,
    HardwarePolicyRunner,
    HardwareRuntime,
    HardwareRuntimeError,
    ObservationEvidence,
    PolicyTaskOutcome,
    RunTrace,
    StopReceipt,
    VerificationReport,
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
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData
from vibe.core.types import FileImageSource, ImageAttachment, ToolStreamEvent
from vibe.utils.paths import file_uri_to_path
from vibe.utils.tool_presentation import ToolEffectKind


class _RoboUIData[TArgs: BaseModel, TResult: BaseModel](ToolUIData[TArgs, TResult]):
    effect_kind = ToolEffectKind.HARDWARE

    @classmethod
    def project_result(cls, result: TResult) -> JsonValue:
        return result.model_dump(mode="json")

    @classmethod
    def get_status_text(cls) -> str:
        return "Monitoring hardware"


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
    BaseTool[RoboDevicesArgs, RoboDevicesResult, RoboDevicesConfig, BaseToolState],
    _RoboUIData[RoboDevicesArgs, RoboDevicesResult],
):
    @classmethod
    def format_call_display(cls, args: RoboDevicesArgs) -> ToolCallDisplay:
        target = f" {args.device_id}" if args.device_id else ""
        return ToolCallDisplay(summary=f"Robo {args.action}{target}")

    @classmethod
    def format_result_display(cls, result: RoboDevicesResult) -> ToolResultDisplay:
        return ToolResultDisplay(success=True, message=f"Robo {result.action}")

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
    task_outcome: Literal["not_verified"] = "not_verified"


class RoboExecuteConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK


class RoboExecute(
    BaseTool[RoboExecuteArgs, RoboExecuteResult, RoboExecuteConfig, BaseToolState],
    _RoboUIData[RoboExecuteArgs, RoboExecuteResult],
):
    @classmethod
    def is_available(cls, config: VibeConfigSchema | None = None) -> bool:
        """Keep the compatibility implementation hidden from the model."""
        del config
        return False

    @classmethod
    def format_call_display(cls, args: RoboExecuteArgs) -> ToolCallDisplay:
        return ToolCallDisplay(summary=f"Executing {args.action} on {args.device_id}")

    @classmethod
    def format_result_display(cls, result: RoboExecuteResult) -> ToolResultDisplay:
        status = result.receipt.status.value
        return ToolResultDisplay(
            success=status == "completed", message=f"Command {status}"
        )

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
    task_outcome: Literal["not_verified"] = "not_verified"


class RoboSequenceConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK


class RoboSequence(
    BaseTool[RoboSequenceArgs, RoboSequenceResult, RoboSequenceConfig, BaseToolState],
    _RoboUIData[RoboSequenceArgs, RoboSequenceResult],
):
    @classmethod
    def is_available(cls, config: VibeConfigSchema | None = None) -> bool:
        """Keep the compatibility implementation hidden from the model."""
        del config
        return False

    @classmethod
    def format_call_display(cls, args: RoboSequenceArgs) -> ToolCallDisplay:
        return ToolCallDisplay(
            summary=f"Executing {len(args.actions)} actions on {args.device_id}"
        )

    @classmethod
    def format_result_display(cls, result: RoboSequenceResult) -> ToolResultDisplay:
        completed = all(
            receipt.status.value == "completed" for receipt in result.receipts
        )
        return ToolResultDisplay(
            success=completed,
            message=f"{len(result.receipts)} hardware actions completed",
        )

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


class RoboRunPolicyArgs(BaseModel):
    device_id: str
    lease_id: str
    code: str = Field(min_length=1, max_length=32 * 1024)
    run_id: str | None = None


class RoboRunPolicyResult(BaseModel):
    policy: HardwarePolicyResult


class RoboRunPolicyConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK
    max_statements: int = Field(default=200, ge=1, le=1000)
    max_actions: int = Field(default=50, ge=1, le=50)
    max_loop_iterations: int = Field(default=50, ge=1, le=1000)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)


class RoboRunPolicy(
    BaseTool[
        RoboRunPolicyArgs, RoboRunPolicyResult, RoboRunPolicyConfig, BaseToolState
    ],
    _RoboUIData[RoboRunPolicyArgs, RoboRunPolicyResult],
):
    @classmethod
    def format_call_display(cls, args: RoboRunPolicyArgs) -> ToolCallDisplay:
        return ToolCallDisplay(summary=f"Running code policy on {args.device_id}")

    @classmethod
    def format_result_display(cls, result: RoboRunPolicyResult) -> ToolResultDisplay:
        return ToolResultDisplay(
            success=result.policy.task_outcome is PolicyTaskOutcome.PASSED,
            message=f"Policy completed · {result.policy.task_outcome.value}",
        )

    async def run(
        self, args: RoboRunPolicyArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | RoboRunPolicyResult, None]:
        run_id = _run_id(args.run_id, ctx)
        config = self.config
        runner = HardwarePolicyRunner(
            _runtime(ctx),
            artifact_dir=_policy_artifact_dir(ctx, self.cwd, run_id),
            max_statements=config.max_statements,
            max_actions=config.max_actions,
            max_loop_iterations=config.max_loop_iterations,
            timeout_seconds=config.timeout_seconds,
        )
        try:
            result = await runner.run(
                args.code,
                device_id=args.device_id,
                lease_id=args.lease_id,
                run_id=run_id,
            )
        except HardwarePolicyError as exc:
            location = (
                f"line={exc.line},column={exc.column}" if exc.line is not None else None
            )
            detail = ", ".join(
                part
                for part in (
                    f"error_code={exc.code}",
                    f"message={exc}",
                    location,
                    f"source_uri={exc.source_uri}" if exc.source_uri else None,
                )
                if part is not None
            )
            raise ToolError("Robo code policy failed", model_detail=detail) from exc
        except HardwareRuntimeError as exc:
            raise ToolError(str(exc), model_detail=f"error_code={exc.code}") from exc
        yield RoboRunPolicyResult(policy=result)

    def get_result_images(self, result: RoboRunPolicyResult) -> list[ImageAttachment]:
        evidence = list(result.policy.snapshot.evidence)
        if result.policy.report is not None:
            evidence.extend(result.policy.report.evidence)
        unique = {item.evidence_id: item for item in evidence}
        return _evidence_images(list(unique.values()))


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
    BaseTool[RoboObserveArgs, RoboObserveResult, RoboObserveConfig, BaseToolState],
    _RoboUIData[RoboObserveArgs, RoboObserveResult],
):
    @classmethod
    def format_call_display(cls, args: RoboObserveArgs) -> ToolCallDisplay:
        target = args.device_id or "runtime"
        return ToolCallDisplay(summary=f"Observing {target}")

    @classmethod
    def format_result_display(cls, result: RoboObserveResult) -> ToolResultDisplay:
        status = result.snapshot.status.value if result.snapshot else "workspace"
        return ToolResultDisplay(success=True, message=f"Observed {status} state")

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

    def get_result_images(self, result: RoboObserveResult) -> list[ImageAttachment]:
        return _evidence_images(result.snapshot.evidence if result.snapshot else [])


class RoboVerifyArgs(BaseModel):
    device_id: str
    criterion: str
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    run_id: str | None = None


class RoboVerifyResult(BaseModel):
    report: VerificationReport


class RoboVerifyConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ALWAYS


class RoboVerify(
    BaseTool[RoboVerifyArgs, RoboVerifyResult, RoboVerifyConfig, BaseToolState],
    _RoboUIData[RoboVerifyArgs, RoboVerifyResult],
):
    @classmethod
    def format_call_display(cls, args: RoboVerifyArgs) -> ToolCallDisplay:
        return ToolCallDisplay(
            summary=f"Verifying {args.criterion} on {args.device_id}"
        )

    @classmethod
    def format_result_display(cls, result: RoboVerifyResult) -> ToolResultDisplay:
        status = result.report.status.value
        return ToolResultDisplay(
            success=status == "passed", message=f"Verification {status}"
        )

    async def run(
        self, args: RoboVerifyArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | RoboVerifyResult, None]:
        runtime = _runtime(ctx)
        try:
            report = await runtime.verify(
                args.device_id,
                criterion=args.criterion,
                parameters=args.parameters,
                run_id=_run_id(args.run_id, ctx),
            )
        except HardwareRuntimeError as exc:
            raise ToolError(str(exc), model_detail=f"error_code={exc.code}") from exc
        yield RoboVerifyResult(report=report)

    def get_result_images(self, result: RoboVerifyResult) -> list[ImageAttachment]:
        return _evidence_images(result.report.evidence)


class RoboStopArgs(BaseModel):
    device_id: str
    reason: str
    run_id: str | None = None


class RoboStopResult(BaseModel):
    receipt: StopReceipt


class RoboStopConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ALWAYS


class RoboStop(
    BaseTool[RoboStopArgs, RoboStopResult, RoboStopConfig, BaseToolState],
    _RoboUIData[RoboStopArgs, RoboStopResult],
):
    @classmethod
    def format_call_display(cls, args: RoboStopArgs) -> ToolCallDisplay:
        return ToolCallDisplay(summary=f"Stopping {args.device_id}")

    @classmethod
    def format_result_display(cls, result: RoboStopResult) -> ToolResultDisplay:
        return ToolResultDisplay(
            success=result.receipt.acknowledged,
            message=(
                "Hardware stopped"
                if result.receipt.acknowledged
                else "Hardware stop unconfirmed"
            ),
        )

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


def _policy_artifact_dir(ctx: InvokeContext | None, cwd: Path, run_id: str) -> Path:
    if ctx is not None and ctx.session_dir is not None:
        return ctx.session_dir / "hardware-runs" / "policies"
    return cwd / ".vibe" / "hardware-runs" / run_id / "policies"


def _required(value: str | None, name: str) -> str:
    if value:
        return value
    raise ToolError(f"{name} is required for this action")


def _evidence_images(
    evidence_items: list[ObservationEvidence],
) -> list[ImageAttachment]:
    images: list[ImageAttachment] = []
    for evidence in evidence_items:
        parsed = urlparse(evidence.uri)
        if parsed.scheme != "file":
            continue
        path = Path(file_uri_to_path(evidence.uri))
        if not path.is_file() or not evidence.mime_type.startswith("image/"):
            continue
        images.append(
            ImageAttachment(
                source=FileImageSource(path=path),
                alias=path.name,
                mime_type=evidence.mime_type,
            )
        )
    return images


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
    "RoboRunPolicy",
    "RoboRunPolicyArgs",
    "RoboRunPolicyConfig",
    "RoboRunPolicyResult",
    "RoboSequence",
    "RoboSequenceAction",
    "RoboSequenceArgs",
    "RoboSequenceConfig",
    "RoboSequenceResult",
    "RoboStop",
    "RoboStopArgs",
    "RoboStopConfig",
    "RoboStopResult",
    "RoboVerify",
    "RoboVerifyArgs",
    "RoboVerifyConfig",
    "RoboVerifyResult",
    "WorkspaceObservation",
]
