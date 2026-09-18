from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
from urllib.parse import urlparse

import anyio
from pydantic import JsonValue

from vibe.core.hardware._adapter_port import DeviceAdapter
from vibe.core.hardware.models import (
    MAX_OBSERVATION_EVIDENCE,
    MAX_TRACE_EVENTS,
    CommandReceipt,
    CommandStatus,
    DeviceCapabilityManifest,
    DeviceLease,
    DeviceSnapshot,
    DeviceStatus,
    HardwareCommand,
    ObservationEvidence,
    RunEvent,
    RunTrace,
    StopReceipt,
    VerificationReport,
    VerificationStatus,
)
from vibe.core.hardware.simulator import DeterministicSimulatorAdapter
from vibe.observability.logging import logger
from vibe.utils.paths import file_uri_to_path

_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MAX_EVIDENCE_TOTAL_BYTES = 10 * 1024 * 1024
_MAX_VERIFICATION_REPORT_BYTES = 64 * 1024
_SUPPORTED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


class HardwareRuntimeError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _VerificationRequirement:
    completed_at: datetime
    minimum_snapshot_sequence: int
    parameters: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _PendingVerification:
    revision: int
    requirements: dict[str, _VerificationRequirement]


class HardwareRuntime:
    def __init__(
        self,
        *,
        adapters: list[DeviceAdapter],
        trace_dir: Path | None = None,
        trace_event_limit: int = 200,
    ) -> None:
        if not 1 <= trace_event_limit <= MAX_TRACE_EVENTS:
            raise ValueError(
                f"trace_event_limit must be between 1 and {MAX_TRACE_EVENTS}"
            )
        self._adapters = {adapter.adapter_id: adapter for adapter in adapters}
        self._device_adapters: dict[str, DeviceAdapter] = {}
        self._manifests: dict[str, DeviceCapabilityManifest] = {}
        self._connected: set[str] = set()
        self._armed: set[str] = set()
        self._leases: dict[str, DeviceLease] = {}
        self._traces: dict[str, deque[RunEvent]] = {}
        self._trace_sequences: dict[str, int] = {}
        self._trace_event_limit = trace_event_limit
        self._busy: set[str] = set()
        self._snapshot_sequences: dict[str, int] = {}
        self._verification_revision = 0
        self._pending_verifications: dict[str, _PendingVerification] = {}
        self._arming: dict[str, asyncio.Task[DeviceSnapshot]] = {}
        self._inflight: dict[str, asyncio.Task[CommandReceipt]] = {}
        self._lock = asyncio.Lock()
        self._discovery_lock = asyncio.Lock()
        self._trace_lock = asyncio.Lock()
        self._trace_dir = trace_dir

    @classmethod
    def simulated(cls, *, trace_dir: Path | None = None) -> HardwareRuntime:
        return cls(adapters=[DeterministicSimulatorAdapter()], trace_dir=trace_dir)

    async def discover(self) -> list[DeviceCapabilityManifest]:
        async with self._discovery_lock:
            discovered: list[DeviceCapabilityManifest] = []
            for adapter in self._adapters.values():
                manifests = await self._call_adapter("discover", adapter.discover())
                for manifest in manifests:
                    if manifest.adapter_id != adapter.adapter_id:
                        raise HardwareRuntimeError(
                            f"Adapter {adapter.adapter_id} returned a mismatched manifest",
                            code="invalid_manifest",
                        )
                    existing = self._device_adapters.get(manifest.device_id)
                    if existing is not None and existing is not adapter:
                        raise HardwareRuntimeError(
                            f"Duplicate device ID: {manifest.device_id}",
                            code="duplicate_device",
                        )
                    self._device_adapters[manifest.device_id] = adapter
                    self._manifests[manifest.device_id] = manifest
                    discovered.append(manifest)
            return sorted(discovered, key=lambda item: item.device_id)

    async def connect(self, device_id: str, *, run_id: str) -> DeviceSnapshot:
        self._validate_run_id(run_id)
        adapter = await self._adapter_for(device_id)
        snapshot = await self._call_adapter("connect", adapter.connect(device_id))
        self._require_device_id(
            expected=device_id, actual=snapshot.device_id, operation="connect"
        )
        async with self._lock:
            self._connected.add(device_id)
            self._armed.discard(device_id)
            self._snapshot_sequences[device_id] = snapshot.sequence
        await self._record(run_id, "device_connected", device_id=device_id)
        return snapshot

    async def arm(
        self, device_id: str, *, lease_id: str, run_id: str
    ) -> DeviceSnapshot:
        self._validate_run_id(run_id)
        adapter = await self._adapter_for(device_id)
        await self._reserve_arm(device_id, lease_id=lease_id)
        await self._record(
            run_id, "arm_requested", device_id=device_id, payload={"lease_id": lease_id}
        )
        task = await self._start_arm(adapter, device_id=device_id)
        try:
            snapshot = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                await asyncio.shield(
                    self.stop(
                        device_id, reason="arming cancelled by caller", run_id=run_id
                    )
                )
            await self._clear_arm_task(device_id, task)
            await self._record(run_id, "arm_cancelled", device_id=device_id)
            raise
        except BaseException:
            await self._clear_arm_task(device_id, task)
            raise
        return await self._finish_arm(
            device_id=device_id,
            lease_id=lease_id,
            run_id=run_id,
            task=task,
            snapshot=snapshot,
        )

    async def _finish_arm(
        self,
        *,
        device_id: str,
        lease_id: str,
        run_id: str,
        task: asyncio.Task[DeviceSnapshot],
        snapshot: DeviceSnapshot,
    ) -> DeviceSnapshot:
        confirmed = (
            snapshot.device_id == device_id and snapshot.status is DeviceStatus.ARMED
        )
        async with self._lock:
            lease = self._leases.get(device_id)
            reservation_active = (
                device_id not in self._busy
                or self._arming.get(device_id) is not task
                or lease is None
                or lease.lease_id != lease_id
            ) is False
            if self._arming.get(device_id) is task:
                self._arming.pop(device_id)
            self._busy.discard(device_id)
            if confirmed and reservation_active:
                self._armed.add(device_id)
                self._snapshot_sequences[device_id] = snapshot.sequence
        if not confirmed:
            await self.stop(
                device_id, reason="adapter did not confirm arming", run_id=run_id
            )
            raise HardwareRuntimeError(
                f"Device {device_id} did not confirm armed state",
                code="arm_unconfirmed",
            )
        if not reservation_active:
            raise HardwareRuntimeError(
                f"Device {device_id} stopped while it was being armed",
                code="stopped_before_start",
            )
        await self._record(run_id, "device_armed", device_id=device_id)
        return snapshot

    async def disarm(self, device_id: str, *, run_id: str) -> DeviceSnapshot:
        self._validate_run_id(run_id)
        adapter = await self._adapter_for(device_id)
        stop_receipt = await self.stop(
            device_id, reason="device disarmed", run_id=run_id
        )
        if not stop_receipt.acknowledged:
            raise HardwareRuntimeError(
                f"Device {device_id} did not acknowledge stop; disarm was not sent",
                code="stop_unconfirmed",
            )
        snapshot = await self._call_adapter("disarm", adapter.disarm(device_id))
        self._require_device_id(
            expected=device_id, actual=snapshot.device_id, operation="disarm"
        )
        async with self._lock:
            self._armed.discard(device_id)
            self._snapshot_sequences[device_id] = snapshot.sequence
        await self._record(run_id, "device_disarmed", device_id=device_id)
        return snapshot

    async def acquire_lease(
        self, device_id: str, *, owner: str, run_id: str, ttl_seconds: float = 300.0
    ) -> DeviceLease:
        self._validate_run_id(run_id)
        if ttl_seconds <= 0:
            raise HardwareRuntimeError(
                "Lease TTL must be greater than zero", code="invalid_lease"
            )
        now = datetime.now(UTC)
        async with self._lock:
            if device_id not in self._connected:
                raise HardwareRuntimeError(
                    f"Device {device_id} is not connected", code="not_connected"
                )
            current = self._leases.get(device_id)
            if current is not None and current.expires_at > now:
                raise HardwareRuntimeError(
                    f"Device {device_id} already has an active lease",
                    code="lease_conflict",
                )
            if current is not None and device_id in self._armed:
                raise HardwareRuntimeError(
                    f"Expired lease for armed device {device_id}; stop it before reacquiring",
                    code="stop_required",
                )
            lease = DeviceLease(
                device_id=device_id,
                owner=owner,
                acquired_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._leases[device_id] = lease
        await self._record(
            run_id,
            "lease_acquired",
            device_id=device_id,
            payload={"lease_id": lease.lease_id, "owner": owner},
        )
        return lease

    async def release_lease(
        self, device_id: str, *, lease_id: str, run_id: str
    ) -> None:
        self._validate_run_id(run_id)
        async with self._lock:
            lease = self._leases.get(device_id)
            if lease is None or lease.lease_id != lease_id:
                raise HardwareRuntimeError(
                    f"No matching active lease for {device_id}", code="lease_required"
                )
            if device_id in self._armed:
                raise HardwareRuntimeError(
                    f"Device {device_id} must be disarmed before releasing its lease",
                    code="disarm_required",
                )
            self._leases.pop(device_id)
        await self._record(
            run_id,
            "lease_released",
            device_id=device_id,
            payload={"lease_id": lease_id},
        )

    async def execute(self, command: HardwareCommand, *, run_id: str) -> CommandReceipt:
        self._validate_run_id(run_id)
        adapter = await self._adapter_for(command.device_id)
        rejection, snapshot_sequence = await self._reserve_command(command)
        if rejection is not None:
            await self._record(
                run_id,
                "command_rejected",
                device_id=command.device_id,
                command_id=command.command_id,
                payload={"reason": rejection},
            )
            messages = {
                "not_connected": f"Device {command.device_id} is not connected",
                "lease_required": (
                    f"Command requires the active lease for {command.device_id}"
                ),
                "not_armed": f"Device {command.device_id} is not armed",
                "unsupported_action": f"Unsupported action: {command.action}",
                "device_busy": f"Device {command.device_id} is already executing",
            }
            raise HardwareRuntimeError(messages[rejection], code=rejection)

        try:
            await self._record(
                run_id,
                "command_started",
                device_id=command.device_id,
                command_id=command.command_id,
                payload={"action": command.action},
            )
        except BaseException:
            async with self._lock:
                self._busy.discard(command.device_id)
            raise

        task: asyncio.Task[CommandReceipt] | None = None
        async with self._lock:
            if command.device_id in self._busy:
                task = asyncio.create_task(adapter.execute(command))
                self._inflight[command.device_id] = task
        if task is None:
            await self._record(
                run_id,
                "command_cancelled",
                device_id=command.device_id,
                command_id=command.command_id,
                payload={"reason": "device stopped before command dispatch"},
            )
            raise HardwareRuntimeError(
                f"Device {command.device_id} stopped before command dispatch",
                code="stopped_before_start",
            )
        try:
            receipt = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                await asyncio.shield(
                    self.stop(
                        command.device_id,
                        reason="command cancelled by caller",
                        run_id=run_id,
                    )
                )
            await self._record(
                run_id,
                "command_cancelled",
                device_id=command.device_id,
                command_id=command.command_id,
            )
            raise
        except Exception as exc:
            await self._record(
                run_id,
                "command_failed",
                device_id=command.device_id,
                command_id=command.command_id,
                payload={"error": type(exc).__name__},
            )
            raise HardwareRuntimeError(
                "Hardware adapter execute failed", code="adapter_error"
            ) from exc
        finally:
            async with self._lock:
                if self._inflight.get(command.device_id) is task:
                    self._inflight.pop(command.device_id)
                self._busy.discard(command.device_id)

        if (
            receipt.device_id != command.device_id
            or receipt.command_id != command.command_id
        ):
            with contextlib.suppress(HardwareRuntimeError):
                await self.stop(
                    command.device_id,
                    reason="adapter returned a mismatched command receipt",
                    run_id=run_id,
                )
            raise HardwareRuntimeError(
                "Hardware adapter execute returned a mismatched receipt",
                code="adapter_contract",
            )

        event_kind = {
            CommandStatus.COMPLETED: "command_completed",
            CommandStatus.FAILED: "command_failed",
            CommandStatus.UNKNOWN: "command_unknown",
        }[receipt.status]
        if receipt.status is CommandStatus.COMPLETED:
            await self._mark_verification_required(
                command, receipt, minimum_snapshot_sequence=snapshot_sequence + 1
            )
        await self._record(
            run_id,
            event_kind,
            device_id=command.device_id,
            command_id=command.command_id,
            payload={"status": receipt.status.value},
        )
        return receipt

    async def _mark_verification_required(
        self,
        command: HardwareCommand,
        receipt: CommandReceipt,
        *,
        minimum_snapshot_sequence: int,
    ) -> None:
        async with self._lock:
            manifest = self._manifests[command.device_id]
            verifications = [
                verification
                for verification in manifest.verifications
                if command.action in verification.applicable_actions
            ]
            if not verifications:
                return
            current = self._pending_verifications.get(command.device_id)
            requirements = dict(current.requirements) if current is not None else {}
            for verification in verifications:
                expected_parameters = {
                    target: command.parameters.get(source)
                    for target, source in verification.parameter_bindings.items()
                }
                requirements[verification.name] = _VerificationRequirement(
                    completed_at=receipt.completed_at,
                    minimum_snapshot_sequence=minimum_snapshot_sequence,
                    parameters=expected_parameters,
                )
            self._verification_revision += 1
            self._pending_verifications[command.device_id] = _PendingVerification(
                revision=self._verification_revision, requirements=requirements
            )

    async def execute_sequence(
        self, commands: list[HardwareCommand], *, run_id: str
    ) -> list[CommandReceipt]:
        self._validate_run_id(run_id)
        receipts: list[CommandReceipt] = []
        try:
            for command in commands:
                receipt = await self.execute(command, run_id=run_id)
                receipts.append(receipt)
                if receipt.status is not CommandStatus.COMPLETED:
                    await self.stop(
                        command.device_id,
                        reason=f"sequence received {receipt.status.value} receipt",
                        run_id=run_id,
                    )
                    break
        except Exception:
            if commands:
                with contextlib.suppress(Exception):
                    await self.stop(
                        commands[0].device_id, reason="sequence failed", run_id=run_id
                    )
            raise
        return receipts

    async def observe(self, device_id: str, *, run_id: str) -> DeviceSnapshot:
        self._validate_run_id(run_id)
        adapter = await self._adapter_for(device_id)
        snapshot = await self._call_adapter("observe", adapter.observe(device_id))
        self._require_device_id(
            expected=device_id, actual=snapshot.device_id, operation="observe"
        )
        self._validate_evidence(snapshot.evidence)
        await self._record_snapshot_sequence(device_id, snapshot.sequence)
        await self._record(
            run_id,
            "device_observed",
            device_id=device_id,
            payload={"status": snapshot.status.value, "sequence": snapshot.sequence},
        )
        return snapshot

    async def verify(
        self,
        device_id: str,
        *,
        criterion: str,
        run_id: str,
        parameters: dict[str, JsonValue] | None = None,
    ) -> VerificationReport:
        self._validate_run_id(run_id)
        adapter = await self._adapter_for(device_id)
        async with self._lock:
            if device_id not in self._connected:
                raise HardwareRuntimeError(
                    f"Device {device_id} is not connected", code="not_connected"
                )
            if device_id in self._busy:
                raise HardwareRuntimeError(
                    f"Device {device_id} is still executing; observe it after it stops",
                    code="device_busy",
                )
            manifest = self._manifests[device_id]
            supported = {item.name for item in manifest.verifications}
            pending = self._pending_verifications.get(device_id)
            pending_revision = pending.revision if pending is not None else None
        if criterion not in supported:
            raise HardwareRuntimeError(
                f"Unsupported verification criterion: {criterion}",
                code="unsupported_verification",
            )
        report = await self._call_adapter(
            "verify", adapter.verify(device_id, criterion, parameters or {})
        )
        self._require_device_id(
            expected=device_id, actual=report.device_id, operation="verify"
        )
        if report.criterion != criterion:
            raise HardwareRuntimeError(
                "Hardware adapter verify returned a mismatched criterion",
                code="adapter_contract",
            )
        if len(report.model_dump_json().encode()) > _MAX_VERIFICATION_REPORT_BYTES:
            raise HardwareRuntimeError(
                "Hardware adapter returned an oversized verification report",
                code="adapter_contract",
            )
        self._validate_evidence(report.evidence)
        returned_evidence_ids = {item.evidence_id for item in report.evidence}
        if len(returned_evidence_ids) != len(report.evidence):
            raise HardwareRuntimeError(
                "Hardware adapter returned duplicate evidence IDs",
                code="adapter_contract",
            )
        referenced_evidence_ids = {
            evidence_id for check in report.checks for evidence_id in check.evidence_ids
        }
        referenced_evidence_ids.update(report.evidence_ids)
        if not referenced_evidence_ids.issubset(returned_evidence_ids):
            raise HardwareRuntimeError(
                "Hardware adapter verification referenced missing evidence",
                code="adapter_contract",
            )
        expected_status = VerificationStatus.PASSED
        if any(check.status is VerificationStatus.FAILED for check in report.checks):
            expected_status = VerificationStatus.FAILED
        elif any(
            check.status is VerificationStatus.INCONCLUSIVE for check in report.checks
        ):
            expected_status = VerificationStatus.INCONCLUSIVE
        if report.status is not expected_status:
            raise HardwareRuntimeError(
                "Hardware adapter verification returned an inconsistent status",
                code="adapter_contract",
            )
        await self._record_snapshot_sequence(device_id, report.snapshot_sequence)
        await self._clear_matching_verification_requirement(
            device_id=device_id,
            criterion=criterion,
            parameters=parameters or {},
            report=report,
            pending_revision=pending_revision,
        )
        await self._record(
            run_id,
            "task_verification_completed",
            device_id=device_id,
            payload={
                "criterion": criterion,
                "status": report.status.value,
                "verification_id": report.verification_id,
            },
        )
        return report

    async def pending_verification_revisions(self) -> dict[str, int]:
        """Return task-changing actions that still require a verify call."""
        async with self._lock:
            return {
                device_id: pending.revision
                for device_id, pending in self._pending_verifications.items()
            }

    async def _clear_matching_verification_requirement(
        self,
        *,
        device_id: str,
        criterion: str,
        parameters: dict[str, JsonValue],
        report: VerificationReport,
        pending_revision: int | None,
    ) -> None:
        async with self._lock:
            pending = self._pending_verifications.get(device_id)
            if pending is None or pending.revision != pending_revision:
                return
            requirement = pending.requirements.get(criterion)
            if requirement is None or parameters != requirement.parameters:
                return
            try:
                stale = report.observed_at < requirement.completed_at or any(
                    evidence.captured_at < requirement.completed_at
                    for evidence in report.evidence
                )
            except TypeError as exc:
                raise HardwareRuntimeError(
                    "Hardware adapter verification returned invalid timestamps",
                    code="adapter_contract",
                ) from exc
            if stale:
                raise HardwareRuntimeError(
                    "Hardware adapter verification predates the completed action",
                    code="adapter_contract",
                )
            if report.snapshot_sequence < requirement.minimum_snapshot_sequence:
                raise HardwareRuntimeError(
                    "Hardware adapter verification used a stale snapshot sequence",
                    code="adapter_contract",
                )
            remaining = dict(pending.requirements)
            remaining.pop(criterion)
            if remaining:
                self._pending_verifications[device_id] = _PendingVerification(
                    revision=pending.revision, requirements=remaining
                )
            else:
                self._pending_verifications.pop(device_id, None)

    async def _record_snapshot_sequence(self, device_id: str, sequence: int) -> None:
        async with self._lock:
            previous = self._snapshot_sequences.get(device_id)
            if previous is not None and sequence < previous:
                raise HardwareRuntimeError(
                    "Hardware adapter returned a regressing snapshot sequence",
                    code="adapter_contract",
                )
            self._snapshot_sequences[device_id] = sequence

    async def stop(self, device_id: str, *, reason: str, run_id: str) -> StopReceipt:
        self._validate_run_id(run_id)
        adapter = await self._adapter_for(device_id)
        await self._record(
            run_id, "stop_requested", device_id=device_id, payload={"reason": reason}
        )
        receipt = await self._call_adapter("stop", adapter.stop(device_id, reason))
        await self._require_stop_receipt_device_id(device_id, receipt)
        if receipt.acknowledged:
            async with self._lock:
                command_task = self._inflight.pop(device_id, None)
                arm_task = self._arming.pop(device_id, None)
                self._busy.discard(device_id)
                self._leases.pop(device_id, None)
                self._armed.discard(device_id)
            tasks = [task for task in (command_task, arm_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            if arm_task is not None:
                receipt = await self._call_adapter(
                    "stop", adapter.stop(device_id, f"{reason}; post-arm confirmation")
                )
                await self._require_stop_receipt_device_id(device_id, receipt)
                if not receipt.acknowledged:
                    await self._invalidate_device(device_id)
            kind = "stop_acknowledged" if receipt.acknowledged else "stop_unconfirmed"
        else:
            kind = "stop_unconfirmed"
        await self._record(
            run_id, kind, device_id=device_id, payload={"message": receipt.message}
        )
        return receipt

    def read_trace(self, run_id: str) -> RunTrace:
        self._validate_run_id(run_id)
        return RunTrace(run_id=run_id, events=list(self._traces.get(run_id, ())))

    async def aclose(self) -> None:
        connected = list(self._connected)
        inflight = [*self._inflight.values(), *self._arming.values()]
        arming_devices = set(self._arming)
        for device_id in connected:
            adapter = self._device_adapters.get(device_id)
            if adapter is None:
                continue
            with contextlib.suppress(Exception):
                await adapter.stop(device_id, "hardware runtime closing")
        for task in inflight:
            if not task.done():
                task.cancel()
        for task in inflight:
            with contextlib.suppress(BaseException):
                await task
        for device_id in arming_devices:
            adapter = self._device_adapters.get(device_id)
            if adapter is None:
                continue
            with contextlib.suppress(Exception):
                await adapter.stop(device_id, "post-arm runtime close confirmation")
        for device_id in connected:
            adapter = self._device_adapters.get(device_id)
            if adapter is None:
                continue
            with contextlib.suppress(Exception):
                await adapter.disconnect(device_id)
        for adapter in self._adapters.values():
            with contextlib.suppress(Exception):
                await adapter.aclose()
        self._connected.clear()
        self._leases.clear()
        self._armed.clear()
        self._busy.clear()
        self._arming.clear()
        self._inflight.clear()

    async def _adapter_for(self, device_id: str) -> DeviceAdapter:
        if adapter := self._device_adapters.get(device_id):
            return adapter
        await self.discover()
        if adapter := self._device_adapters.get(device_id):
            return adapter
        raise HardwareRuntimeError(f"Unknown device: {device_id}", code="not_found")

    async def _reserve_command(
        self, command: HardwareCommand
    ) -> tuple[str | None, int]:
        now = datetime.now(UTC)
        async with self._lock:
            if command.device_id not in self._connected:
                return "not_connected", -1
            lease = self._leases.get(command.device_id)
            if (
                lease is None
                or lease.lease_id != command.lease_id
                or lease.expires_at <= now
            ):
                return "lease_required", -1
            if command.device_id not in self._armed:
                return "not_armed", -1
            manifest = self._manifests[command.device_id]
            if command.action not in {action.name for action in manifest.actions}:
                return "unsupported_action", -1
            if command.device_id in self._busy:
                return "device_busy", -1
            self._busy.add(command.device_id)
            snapshot_sequence = self._snapshot_sequences.get(command.device_id, -1)
        return None, snapshot_sequence

    async def _reserve_arm(self, device_id: str, *, lease_id: str) -> None:
        async with self._lock:
            rejection = self._control_rejection(device_id, lease_id=lease_id)
            if rejection is not None:
                raise HardwareRuntimeError(
                    f"Cannot arm {device_id}: {rejection}", code=rejection
                )
            if device_id in self._busy:
                raise HardwareRuntimeError(
                    f"Device {device_id} is already executing", code="device_busy"
                )
            self._busy.add(device_id)

    async def _start_arm(
        self, adapter: DeviceAdapter, *, device_id: str
    ) -> asyncio.Task[DeviceSnapshot]:
        async with self._lock:
            if device_id not in self._busy:
                raise HardwareRuntimeError(
                    f"Device {device_id} stopped before arming",
                    code="stopped_before_start",
                )
            task = asyncio.create_task(
                self._call_adapter("arm", adapter.arm(device_id))
            )
            self._arming[device_id] = task
            return task

    async def _clear_arm_task(
        self, device_id: str, task: asyncio.Task[DeviceSnapshot]
    ) -> None:
        async with self._lock:
            if self._arming.get(device_id) is task:
                self._arming.pop(device_id)
            self._busy.discard(device_id)

    async def _invalidate_device(self, device_id: str) -> None:
        async with self._lock:
            tasks = [
                task
                for task in (
                    self._inflight.pop(device_id, None),
                    self._arming.pop(device_id, None),
                )
                if task is not None
            ]
            self._connected.discard(device_id)
            self._leases.pop(device_id, None)
            self._armed.discard(device_id)
            self._busy.discard(device_id)
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

    async def _require_stop_receipt_device_id(
        self, device_id: str, receipt: StopReceipt
    ) -> None:
        if receipt.device_id == device_id:
            return
        await self._invalidate_device(device_id)
        raise HardwareRuntimeError(
            "Hardware adapter stop returned a mismatched receipt",
            code="adapter_contract",
        )

    def _control_rejection(self, device_id: str, *, lease_id: str) -> str | None:
        if device_id not in self._connected:
            return "not_connected"
        lease = self._leases.get(device_id)
        if (
            lease is None
            or lease.lease_id != lease_id
            or lease.expires_at <= datetime.now(UTC)
        ):
            return "lease_required"
        return None

    @staticmethod
    async def _call_adapter[ResultT](
        operation: str, call: Awaitable[ResultT]
    ) -> ResultT:
        try:
            return await call
        except Exception as exc:
            raise HardwareRuntimeError(
                f"Hardware adapter {operation} failed", code="adapter_error"
            ) from exc

    @staticmethod
    def _require_device_id(*, expected: str, actual: str, operation: str) -> None:
        if actual != expected:
            raise HardwareRuntimeError(
                f"Hardware adapter {operation} returned a mismatched device ID",
                code="adapter_contract",
            )

    def _validate_evidence(self, evidence_items: list[ObservationEvidence]) -> None:
        if not evidence_items:
            return
        if len(evidence_items) > MAX_OBSERVATION_EVIDENCE:
            raise HardwareRuntimeError(
                "Hardware adapter returned too many observation evidence items",
                code="adapter_contract",
            )
        if self._trace_dir is None:
            raise HardwareRuntimeError(
                "Hardware adapter returned evidence without a trusted evidence directory",
                code="adapter_contract",
            )
        evidence_root = (self._trace_dir / "evidence").resolve()
        total_bytes = 0
        for evidence in evidence_items:
            parsed = urlparse(evidence.uri)
            if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
                raise HardwareRuntimeError(
                    "Hardware adapter returned untrusted observation evidence",
                    code="adapter_contract",
                )
            path = Path(file_uri_to_path(evidence.uri)).resolve()
            if (
                not path.is_relative_to(evidence_root)
                or evidence.mime_type not in _SUPPORTED_IMAGE_TYPES
                or not path.is_file()
            ):
                raise HardwareRuntimeError(
                    "Hardware adapter returned untrusted observation evidence",
                    code="adapter_contract",
                )
            total_bytes += path.stat().st_size
            if total_bytes > _MAX_EVIDENCE_TOTAL_BYTES:
                raise HardwareRuntimeError(
                    "Hardware adapter returned too much observation evidence",
                    code="adapter_contract",
                )

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if _RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise HardwareRuntimeError(
                "run_id must be 1-128 letters, numbers, dots, underscores, or dashes",
                code="invalid_run_id",
            )

    async def _record(
        self,
        run_id: str,
        kind: str,
        *,
        device_id: str | None = None,
        command_id: str | None = None,
        payload: dict[str, JsonValue] | None = None,
    ) -> None:
        self._validate_run_id(run_id)
        async with self._trace_lock:
            events = self._traces.setdefault(
                run_id, deque(maxlen=self._trace_event_limit)
            )
            sequence = self._trace_sequences.get(run_id, 0) + 1
            self._trace_sequences[run_id] = sequence
            event = RunEvent(
                sequence=sequence,
                run_id=run_id,
                kind=kind,
                occurred_at=datetime.now(UTC),
                device_id=device_id,
                command_id=command_id,
                payload=payload or {},
            )
            events.append(event)
            if self._trace_dir is not None:
                try:
                    await self._append_trace(event)
                except Exception as exc:
                    logger.warning(
                        "Failed to persist hardware trace run_id=%s kind=%s error=%s",
                        run_id,
                        kind,
                        type(exc).__name__,
                    )

    async def _append_trace(self, event: RunEvent) -> None:
        trace_root = self._trace_dir
        if trace_root is None:
            return
        trace_dir = anyio.Path(trace_root)
        await trace_dir.mkdir(parents=True, exist_ok=True)
        path = trace_dir / f"{event.run_id}.jsonl"
        async with await path.open("a", encoding="utf-8") as stream:
            await stream.write(event.model_dump_json() + "\n")


__all__ = ["HardwareRuntime", "HardwareRuntimeError"]
