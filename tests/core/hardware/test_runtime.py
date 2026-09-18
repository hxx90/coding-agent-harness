from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue, ValidationError
import pytest

from vibe.core.hardware import (
    CommandReceipt,
    CommandStatus,
    DeterministicSimulatorAdapter,
    DeviceSnapshot,
    HardwareCommand,
    HardwareRuntime,
    HardwareRuntimeError,
    ObservationEvidence,
    ObservationModality,
    RunEvent,
    StopReceipt,
    VerificationReport,
    VerificationStatus,
)


class BlockingSimulatorAdapter(DeterministicSimulatorAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stop_acknowledged = False

    async def execute(self, command: HardwareCommand) -> CommandReceipt:
        self.started.set()
        await self.release.wait()
        return await super().execute(command)

    async def stop(self, device_id: str, reason: str):
        receipt = await super().stop(device_id, reason)
        self.stop_acknowledged = receipt.acknowledged
        return receipt


class FailingReceiptAdapter(DeterministicSimulatorAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.executed: list[str] = []

    async def execute(self, command: HardwareCommand) -> CommandReceipt:
        self.executed.append(command.command_id)
        now = datetime.now(UTC)
        return CommandReceipt(
            command_id=command.command_id,
            device_id=command.device_id,
            status=CommandStatus.FAILED,
            started_at=now,
            completed_at=now,
            error="simulated NACK",
        )


class UnconfirmedStopAdapter(DeterministicSimulatorAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.disarm_called = False

    async def stop(self, device_id: str, reason: str) -> StopReceipt:
        self._validate_device(device_id)
        return StopReceipt(
            device_id=device_id,
            acknowledged=False,
            observed_at=datetime.now(UTC),
            message=f"stop not confirmed: {reason}",
        )

    async def disarm(self, device_id: str):
        self.disarm_called = True
        return await super().disarm(device_id)


class UntrustedEvidenceAdapter(DeterministicSimulatorAdapter):
    def __init__(self, evidence_path: Path) -> None:
        super().__init__()
        self.evidence_path = evidence_path

    async def observe(self, device_id: str) -> DeviceSnapshot:
        snapshot = await super().observe(device_id)
        return snapshot.model_copy(
            update={
                "evidence": [
                    ObservationEvidence(
                        source_id="camera",
                        modality=ObservationModality.RGB,
                        captured_at=datetime.now(UTC),
                        sequence=snapshot.sequence,
                        mime_type="image/png",
                        uri=self.evidence_path.as_uri(),
                    )
                ]
            }
        )


class ExcessiveEvidenceAdapter(DeterministicSimulatorAdapter):
    def __init__(self, evidence_paths: list[Path]) -> None:
        super().__init__()
        self.evidence_paths = evidence_paths

    async def observe(self, device_id: str) -> DeviceSnapshot:
        snapshot = await super().observe(device_id)
        evidence = [
            ObservationEvidence(
                source_id="camera",
                modality=ObservationModality.RGB,
                captured_at=datetime.now(UTC),
                sequence=snapshot.sequence,
                mime_type="image/png",
                uri=path.as_uri(),
            )
            for path in self.evidence_paths
        ]
        return snapshot.model_copy(update={"evidence": evidence})


class InvalidVerificationAdapter(DeterministicSimulatorAdapter):
    def __init__(self, *, missing_evidence: bool = False) -> None:
        super().__init__()
        self.missing_evidence = missing_evidence

    async def verify(
        self, device_id: str, criterion: str, parameters: dict[str, JsonValue]
    ) -> VerificationReport:
        report = await super().verify(device_id, criterion, parameters)
        if self.missing_evidence:
            checks = [
                report.checks[0].model_copy(
                    update={"evidence_ids": ["missing-evidence"]}
                )
            ]
            return report.model_copy(update={"checks": checks})
        return report.model_copy(update={"status": VerificationStatus.PASSED})


class StaleVerificationAdapter(DeterministicSimulatorAdapter):
    async def verify(
        self, device_id: str, criterion: str, parameters: dict[str, JsonValue]
    ) -> VerificationReport:
        report = await super().verify(device_id, criterion, parameters)
        return report.model_copy(
            update={"observed_at": datetime(2000, 1, 1, tzinfo=UTC)}
        )


class OversizedVerificationAdapter(DeterministicSimulatorAdapter):
    async def verify(
        self, device_id: str, criterion: str, parameters: dict[str, JsonValue]
    ) -> VerificationReport:
        report = await super().verify(device_id, criterion, parameters)
        oversized_check = report.checks[0].model_copy(update={"observed": "x" * 70_000})
        return report.model_copy(update={"checks": [oversized_check]})


class CancellationResistantArmAdapter(DeterministicSimulatorAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.arm_started = asyncio.Event()
        self.finish_arm = asyncio.Event()
        self.first_stop_done = asyncio.Event()
        self.stop_count = 0

    async def arm(self, device_id: str):
        self.arm_started.set()
        try:
            await self.finish_arm.wait()
        except asyncio.CancelledError:
            await self.finish_arm.wait()
        return await super().arm(device_id)

    async def stop(self, device_id: str, reason: str) -> StopReceipt:
        self.stop_count += 1
        receipt = await super().stop(device_id, reason)
        self.first_stop_done.set()
        return receipt


class UnconfirmedArmAdapter(DeterministicSimulatorAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.stop_called = False

    async def arm(self, device_id: str):
        self._validate_connected(device_id)
        return self._snapshot()

    async def stop(self, device_id: str, reason: str) -> StopReceipt:
        self.stop_called = True
        return await super().stop(device_id, reason)


class MismatchedReceiptAdapter(DeterministicSimulatorAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.stop_called = False

    async def execute(self, command: HardwareCommand) -> CommandReceipt:
        receipt = await super().execute(command)
        return receipt.model_copy(update={"command_id": "wrong-command"})

    async def stop(self, device_id: str, reason: str) -> StopReceipt:
        self.stop_called = True
        return await super().stop(device_id, reason)


class MismatchedStopAdapter(DeterministicSimulatorAdapter):
    async def stop(self, device_id: str, reason: str) -> StopReceipt:
        receipt = await super().stop(device_id, reason)
        return receipt.model_copy(update={"device_id": "wrong-device"})


class MismatchedPostArmStopAdapter(CancellationResistantArmAdapter):
    async def stop(self, device_id: str, reason: str) -> StopReceipt:
        receipt = await super().stop(device_id, reason)
        if self.stop_count == 2:
            return receipt.model_copy(update={"device_id": "wrong-device"})
        return receipt


class UnconfirmedPostArmStopAdapter(CancellationResistantArmAdapter):
    async def stop(self, device_id: str, reason: str) -> StopReceipt:
        receipt = await super().stop(device_id, reason)
        if self.stop_count == 2:
            return receipt.model_copy(update={"acknowledged": False})
        return receipt


@pytest.mark.asyncio
async def test_simulated_arm_completes_a_safe_fold_run(tmp_path: Path) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path / "traces")

    manifests = await runtime.discover()
    assert [(item.device_id, item.model) for item in manifests] == [
        ("sim-arm-1", "Robo Deterministic Arm")
    ]

    connected = await runtime.connect("sim-arm-1", run_id="fold-run")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="local-test", run_id="fold-run"
    )
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="fold-run")
    receipt = await runtime.execute(
        HardwareCommand(
            command_id="fold-command",
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            action="fold_cloth",
            parameters={"cloth": "blue-shirt"},
        ),
        run_id="fold-run",
    )
    snapshot = await runtime.observe("sim-arm-1", run_id="fold-run")
    stopped = await runtime.stop("sim-arm-1", reason="task complete", run_id="fold-run")
    stopped_snapshot = await runtime.observe("sim-arm-1", run_id="fold-run")
    trace = runtime.read_trace("fold-run")

    assert connected.state["mode"] == "idle"
    assert receipt.status == "completed"
    assert snapshot.state["folded_cloths"] == ["blue-shirt"]
    assert stopped.acknowledged is True
    assert stopped_snapshot.status == "stopped"
    assert stopped_snapshot.state["mode"] == "stopped"
    assert [event.kind for event in trace.events] == [
        "device_connected",
        "lease_acquired",
        "arm_requested",
        "device_armed",
        "command_started",
        "command_completed",
        "device_observed",
        "stop_requested",
        "stop_acknowledged",
        "device_observed",
    ]
    assert (tmp_path / "traces" / "fold-run.jsonl").is_file()

    await runtime.aclose()


@pytest.mark.asyncio
async def test_completed_action_requires_a_followup_verification_call(
    tmp_path: Path,
) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    await runtime.connect("sim-arm-1", run_id="verification-guard")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="test", run_id="verification-guard"
    )
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="verification-guard")
    await runtime.execute(
        HardwareCommand(
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            action="pick",
            parameters={"object": "cube"},
        ),
        run_id="verification-guard",
    )

    pending = await runtime.pending_verification_revisions()
    assert set(pending) == {"sim-arm-1"}

    unrelated_report = await runtime.verify(
        "sim-arm-1",
        criterion="cloth_folded",
        parameters={"cloth": "shirt"},
        run_id="verification-guard",
    )
    assert unrelated_report.status == "failed"
    assert set(await runtime.pending_verification_revisions()) == {"sim-arm-1"}

    report = await runtime.verify(
        "sim-arm-1",
        criterion="holding_object",
        parameters={"object": "cube"},
        run_id="verification-guard",
    )
    assert report.status == "passed"
    assert await runtime.pending_verification_revisions() == {}
    verification_event = runtime.read_trace("verification-guard").events[-1]
    assert verification_event.kind == "task_verification_completed"
    assert verification_event.payload["status"] == "passed"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_stale_verification_cannot_clear_a_completed_action(
    tmp_path: Path,
) -> None:
    runtime = HardwareRuntime(adapters=[StaleVerificationAdapter()], trace_dir=tmp_path)
    await runtime.connect("sim-arm-1", run_id="stale-verification")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="test", run_id="stale-verification"
    )
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="stale-verification")
    await runtime.execute(
        HardwareCommand(
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            action="pick",
            parameters={"object": "cube"},
        ),
        run_id="stale-verification",
    )

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.verify(
            "sim-arm-1",
            criterion="holding_object",
            parameters={"object": "cube"},
            run_id="stale-verification",
        )

    assert error.value.code == "adapter_contract"
    assert set(await runtime.pending_verification_revisions()) == {"sim-arm-1"}
    await runtime.aclose()


@pytest.mark.asyncio
async def test_motion_without_the_active_lease_is_rejected(tmp_path: Path) -> None:
    runtime = HardwareRuntime(
        adapters=[DeterministicSimulatorAdapter()], trace_dir=tmp_path
    )
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="unsafe-run")

    with pytest.raises(HardwareRuntimeError, match="active lease"):
        await runtime.execute(
            HardwareCommand(
                command_id="unsafe-command",
                device_id="sim-arm-1",
                lease_id="missing",
                action="home",
            ),
            run_id="unsafe-run",
        )

    trace = runtime.read_trace("unsafe-run")
    assert trace.events[-1].kind == "command_rejected"
    assert trace.events[-1].payload["reason"] == "lease_required"

    await runtime.aclose()


@pytest.mark.asyncio
async def test_motion_requires_explicit_arming(tmp_path: Path) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="arm-run")
    lease = await runtime.acquire_lease("sim-arm-1", owner="test", run_id="arm-run")

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.execute(
            HardwareCommand(
                device_id="sim-arm-1", lease_id=lease.lease_id, action="home"
            ),
            run_id="arm-run",
        )

    assert error.value.code == "not_armed"
    armed = await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="arm-run")
    assert armed.status == "armed"

    receipt = await runtime.execute(
        HardwareCommand(device_id="sim-arm-1", lease_id=lease.lease_id, action="home"),
        run_id="arm-run",
    )
    assert receipt.status == "completed"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_discovery_is_idempotent(tmp_path: Path) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path)

    first = await runtime.discover()
    second = await runtime.discover()

    assert first == second
    await runtime.aclose()


@pytest.mark.asyncio
async def test_cancelling_a_command_also_requests_adapter_stop(tmp_path: Path) -> None:
    adapter = BlockingSimulatorAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="cancel-run")
    lease = await runtime.acquire_lease("sim-arm-1", owner="test", run_id="cancel-run")
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="cancel-run")
    task = asyncio.create_task(
        runtime.execute(
            HardwareCommand(
                device_id="sim-arm-1", lease_id=lease.lease_id, action="home"
            ),
            run_id="cancel-run",
        )
    )
    await adapter.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.stop_acknowledged is True
    assert "stop_acknowledged" in [
        event.kind for event in runtime.read_trace("cancel-run").events
    ]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_closing_runtime_stops_and_cancels_inflight_command(
    tmp_path: Path,
) -> None:
    adapter = BlockingSimulatorAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="close-run")
    lease = await runtime.acquire_lease("sim-arm-1", owner="test", run_id="close-run")
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="close-run")
    task = asyncio.create_task(
        runtime.execute(
            HardwareCommand(
                device_id="sim-arm-1", lease_id=lease.lease_id, action="home"
            ),
            run_id="close-run",
        )
    )
    await adapter.started.wait()

    await runtime.aclose()

    assert adapter.stop_acknowledged is True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.1)


@pytest.mark.asyncio
async def test_concurrent_motion_is_rejected_while_device_is_busy(
    tmp_path: Path,
) -> None:
    adapter = BlockingSimulatorAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="busy-run")
    lease = await runtime.acquire_lease("sim-arm-1", owner="test", run_id="busy-run")
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="busy-run")
    first = asyncio.create_task(
        runtime.execute(
            HardwareCommand(
                command_id="first",
                device_id="sim-arm-1",
                lease_id=lease.lease_id,
                action="home",
            ),
            run_id="busy-run",
        )
    )
    await adapter.started.wait()

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.execute(
            HardwareCommand(
                command_id="second",
                device_id="sim-arm-1",
                lease_id=lease.lease_id,
                action="home",
            ),
            run_id="busy-run",
        )

    assert error.value.code == "device_busy"
    adapter.release.set()
    await first
    await runtime.aclose()


@pytest.mark.asyncio
async def test_sequence_stops_after_a_failed_receipt(tmp_path: Path) -> None:
    adapter = FailingReceiptAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="failed-sequence")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="test", run_id="failed-sequence"
    )
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="failed-sequence")
    commands = [
        HardwareCommand(
            command_id=command_id,
            device_id="sim-arm-1",
            lease_id=lease.lease_id,
            action="home",
        )
        for command_id in ("first", "must-not-run")
    ]

    receipts = await runtime.execute_sequence(commands, run_id="failed-sequence")

    assert [receipt.status for receipt in receipts] == [CommandStatus.FAILED]
    assert adapter.executed == ["first"]
    assert (await runtime.observe("sim-arm-1", run_id="failed-sequence")).status == (
        "stopped"
    )
    assert "command_failed" in [
        event.kind for event in runtime.read_trace("failed-sequence").events
    ]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_run_id_cannot_escape_the_trace_directory(tmp_path: Path) -> None:
    runtime = HardwareRuntime.simulated(trace_dir=tmp_path / "traces")

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.connect("sim-arm-1", run_id="../../outside")

    assert error.value.code == "invalid_run_id"
    assert not (tmp_path / "outside.jsonl").exists()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_model_visible_trace_is_bounded(tmp_path: Path) -> None:
    runtime = HardwareRuntime(
        adapters=[DeterministicSimulatorAdapter()],
        trace_dir=tmp_path,
        trace_event_limit=3,
    )
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="bounded-run")
    lease = await runtime.acquire_lease("sim-arm-1", owner="test", run_id="bounded-run")
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="bounded-run")
    await runtime.observe("sim-arm-1", run_id="bounded-run")

    trace = runtime.read_trace("bounded-run")

    assert len(trace.events) == 3
    assert [event.sequence for event in trace.events] == [3, 4, 5]
    await runtime.aclose()


def test_trace_event_rejects_an_unbounded_payload() -> None:
    with pytest.raises(ValidationError, match="payload exceeds"):
        RunEvent(
            sequence=1,
            run_id="bounded-run",
            kind="test",
            occurred_at=datetime.now(UTC),
            payload={"detail": "x" * 20_000},
        )


@pytest.mark.asyncio
async def test_disarm_does_not_continue_after_unconfirmed_stop(tmp_path: Path) -> None:
    adapter = UnconfirmedStopAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="disarm-run")
    lease = await runtime.acquire_lease("sim-arm-1", owner="test", run_id="disarm-run")
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="disarm-run")

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.disarm("sim-arm-1", run_id="disarm-run")

    assert error.value.code == "stop_unconfirmed"
    assert adapter.disarm_called is False
    await runtime.aclose()


@pytest.mark.asyncio
async def test_stop_wins_a_race_with_cancellation_resistant_arming(
    tmp_path: Path,
) -> None:
    adapter = CancellationResistantArmAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="arm-stop-race")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="test", run_id="arm-stop-race"
    )
    arm_task = asyncio.create_task(
        runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="arm-stop-race")
    )
    await adapter.arm_started.wait()
    stop_task = asyncio.create_task(
        runtime.stop("sim-arm-1", reason="operator stop", run_id="arm-stop-race")
    )
    await adapter.first_stop_done.wait()
    adapter.finish_arm.set()

    stopped = await stop_task
    with pytest.raises(HardwareRuntimeError) as error:
        await arm_task

    assert error.value.code == "stopped_before_start"
    assert stopped.acknowledged is True
    assert adapter.stop_count == 2
    assert (await adapter.observe("sim-arm-1")).status == "stopped"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_arm_requires_adapter_confirmation(tmp_path: Path) -> None:
    adapter = UnconfirmedArmAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="arm-ack")
    lease = await runtime.acquire_lease("sim-arm-1", owner="test", run_id="arm-ack")

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="arm-ack")

    assert error.value.code == "arm_unconfirmed"
    assert adapter.stop_called is True
    assert (await adapter.observe("sim-arm-1")).status == "stopped"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_stop_is_not_blocked_by_trace_storage_failure(tmp_path: Path) -> None:
    trace_dir = tmp_path / "traces"
    adapter = DeterministicSimulatorAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=trace_dir)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="trace-failure")
    trace_dir.rename(tmp_path / "old-traces")
    trace_dir.write_text("not a directory")

    receipt = await runtime.stop(
        "sim-arm-1", reason="operator stop", run_id="trace-failure"
    )

    assert receipt.acknowledged is True
    assert (await adapter.observe("sim-arm-1")).status == "stopped"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_mismatched_command_receipt_stops_device(tmp_path: Path) -> None:
    adapter = MismatchedReceiptAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="bad-receipt")
    lease = await runtime.acquire_lease("sim-arm-1", owner="test", run_id="bad-receipt")
    await runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="bad-receipt")

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.execute(
            HardwareCommand(
                command_id="expected-command",
                device_id="sim-arm-1",
                lease_id=lease.lease_id,
                action="home",
            ),
            run_id="bad-receipt",
        )

    assert error.value.code == "adapter_contract"
    assert adapter.stop_called is True
    await runtime.aclose()


@pytest.mark.asyncio
async def test_mismatched_stop_receipt_is_not_accepted(tmp_path: Path) -> None:
    adapter = MismatchedStopAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="bad-stop")

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.stop("sim-arm-1", reason="operator stop", run_id="bad-stop")

    assert error.value.code == "adapter_contract"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_mismatched_post_arm_stop_receipt_is_not_accepted(tmp_path: Path) -> None:
    adapter = MismatchedPostArmStopAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="bad-post-arm-stop")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="test", run_id="bad-post-arm-stop"
    )
    arm_task = asyncio.create_task(
        runtime.arm("sim-arm-1", lease_id=lease.lease_id, run_id="bad-post-arm-stop")
    )
    await adapter.arm_started.wait()
    stop_task = asyncio.create_task(
        runtime.stop("sim-arm-1", reason="operator stop", run_id="bad-post-arm-stop")
    )
    await adapter.first_stop_done.wait()
    adapter.finish_arm.set()

    with pytest.raises(HardwareRuntimeError) as stop_error:
        await stop_task
    with pytest.raises(HardwareRuntimeError) as arm_error:
        await arm_task

    assert stop_error.value.code == "adapter_contract"
    assert arm_error.value.code == "stopped_before_start"
    assert adapter.stop_count == 2
    await runtime.aclose()


@pytest.mark.asyncio
async def test_unconfirmed_post_arm_stop_invalidates_device(tmp_path: Path) -> None:
    adapter = UnconfirmedPostArmStopAdapter()
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="unconfirmed-post-arm-stop")
    lease = await runtime.acquire_lease(
        "sim-arm-1", owner="test", run_id="unconfirmed-post-arm-stop"
    )
    arm_task = asyncio.create_task(
        runtime.arm(
            "sim-arm-1", lease_id=lease.lease_id, run_id="unconfirmed-post-arm-stop"
        )
    )
    await adapter.arm_started.wait()
    stop_task = asyncio.create_task(
        runtime.stop(
            "sim-arm-1", reason="operator stop", run_id="unconfirmed-post-arm-stop"
        )
    )
    await adapter.first_stop_done.wait()
    adapter.finish_arm.set()

    receipt = await stop_task
    with pytest.raises(HardwareRuntimeError) as arm_error:
        await arm_task
    with pytest.raises(HardwareRuntimeError) as lease_error:
        await runtime.acquire_lease(
            "sim-arm-1", owner="retry", run_id="unconfirmed-post-arm-stop"
        )

    assert receipt.acknowledged is False
    assert arm_error.value.code == "stopped_before_start"
    assert lease_error.value.code == "not_connected"
    assert adapter.stop_count == 2
    await runtime.aclose()


@pytest.mark.asyncio
async def test_observe_rejects_evidence_outside_the_runtime_directory(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"not an observation")
    trace_dir = tmp_path / "traces"
    adapter = UntrustedEvidenceAdapter(secret)
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=trace_dir)
    await runtime.discover()
    await runtime.connect("sim-arm-1", run_id="unsafe-evidence")

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.observe("sim-arm-1", run_id="unsafe-evidence")

    assert error.value.code == "adapter_contract"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_observe_rejects_evidence_over_the_total_size_limit(
    tmp_path: Path,
) -> None:
    trace_dir = tmp_path / "traces"
    evidence_dir = trace_dir / "evidence"
    evidence_dir.mkdir(parents=True)
    evidence_paths = [evidence_dir / "first.png", evidence_dir / "second.png"]
    for path in evidence_paths:
        path.write_bytes(b"0" * (6 * 1024 * 1024))
    runtime = HardwareRuntime(
        adapters=[ExcessiveEvidenceAdapter(evidence_paths)], trace_dir=trace_dir
    )
    await runtime.connect("sim-arm-1", run_id="excessive-evidence")

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.observe("sim-arm-1", run_id="excessive-evidence")

    assert error.value.code == "adapter_contract"
    await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_evidence", [False, True])
async def test_verify_rejects_inconsistent_adapter_reports(
    tmp_path: Path, *, missing_evidence: bool
) -> None:
    adapter = InvalidVerificationAdapter(missing_evidence=missing_evidence)
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.connect("sim-arm-1", run_id="invalid-verification")

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.verify(
            "sim-arm-1",
            criterion="holding_object",
            parameters={"object": "cube"},
            run_id="invalid-verification",
        )

    assert error.value.code == "adapter_contract"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_verify_rejects_an_oversized_report(tmp_path: Path) -> None:
    runtime = HardwareRuntime(
        adapters=[OversizedVerificationAdapter()], trace_dir=tmp_path
    )
    await runtime.connect("sim-arm-1", run_id="oversized-verification")

    with pytest.raises(HardwareRuntimeError) as error:
        await runtime.verify(
            "sim-arm-1",
            criterion="holding_object",
            parameters={"object": "cube"},
            run_id="oversized-verification",
        )

    assert error.value.code == "adapter_contract"
    await runtime.aclose()
