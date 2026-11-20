from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Button, Static

from vibe.app_server.events import (
    AppServerEvent,
    HistoryEntryAdded,
    HistoryEntryUpdated,
    SessionSnapshot,
    TurnCompleted,
    TurnStarted,
)
from vibe.app_server.models import (
    BlockedEffectState,
    CancelledEffectState,
    CompletedEffectState,
    FailedEffectState,
    HardwareCleanupResult,
    HardwareCommandReceiptOutput,
    HardwareEffectDetail,
    HardwareEffectOutput,
    HardwareSnapshotOutput,
    HardwareStopReceiptOutput,
    HardwareVerificationOutput,
    PublicEffectEntry,
    PublicMessageEntry,
    PublicReasoningEntry,
    PublicTurn,
    PublicTurnStatus,
    SkippedEffectState,
)


@dataclass
class HardwareProgress:
    """A presentation-only summary derived from public effects, scoped to a turn."""

    effects: dict[str, PublicEffectEntry] = field(default_factory=dict)
    phase: str = "等待设备事件"
    control: str = "尚无动作回执"
    task: str = "未独立验证"
    stop: str = "未确认"
    lease: str = "未确认释放"
    failure: str = ""
    verification: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    device: str = ""

    def update(self, entry: PublicEffectEntry) -> None:
        self.effects[entry.id] = entry
        # Rebuild from canonical entries: duplicate patches never double-count
        # actions, and a completed entry loaded from history works identically.
        rebuilt = HardwareProgress(effects=self.effects)
        for effect in self.effects.values():
            rebuilt._apply(effect)
        self.__dict__.update(rebuilt.__dict__)

    def _apply(self, entry: PublicEffectEntry) -> None:
        if not isinstance(entry.detail, HardwareEffectDetail):
            return
        args = entry.detail.input
        if args and args.device_id:
            if self.device and self.device != args.device_id:
                # Do not combine verification/cleanup from different devices.
                self.task = "未独立验证"
                self.stop = "未确认"
                self.lease = "未确认释放"
                self.control = "尚无动作回执"
            self.device = args.device_id
        state = entry.state
        if args and (
            args.code is not None
            or args.actions
            or args.action in {"move_to", "move_cartesian", "gripper", "home", "arm"}
        ):
            self.task = "动作后待重新验证"
            self.stop = "未确认"
        if isinstance(state, BlockedEffectState):
            self.phase = "等待操作确认"
        elif isinstance(state, CancelledEffectState | SkippedEffectState):
            self.phase = (
                "已取消" if isinstance(state, CancelledEffectState) else "已跳过"
            )
            self.failure = state.reason
        elif isinstance(state, FailedEffectState):
            self.phase = "工具失败"
            self.failure = f"{entry.detail.display.summary}: {state.error.message}"
            if source := re.search(r"source_uri=(file://[^\s,]+)", state.error.message):
                self.artifacts["source"] = source.group(1)
        elif isinstance(state, CompletedEffectState):
            self.phase = "模型正在生成下一步"
            try:
                output = HardwareEffectOutput.model_validate(state.output)
            except ValidationError:
                self.failure = "无法解析设备回执；请展开原始工具结果"
                return
            self._output(output)
        else:
            self.phase = f"执行中：{entry.detail.display.summary}"
            text = getattr(state, "output_text", "")
            if text:
                self.phase = _live_phase(text.strip().splitlines()[-1])

    def _output(self, output: HardwareEffectOutput) -> None:
        if output.lease:
            self.lease = "已持有，待释放"
        if output.released is True:
            self.lease = "已释放"
        self._snapshot(output.snapshot)
        for receipt in output.receipts or []:
            self._receipt(receipt)
        if output.receipt:
            self._receipt(output.receipt)
        if policy := output.policy:
            self.artifacts["source"] = policy.source_uri
            for receipt in policy.receipts:
                self._receipt(receipt)
                if receipt.command_id in self.failure:
                    for step in policy.steps:
                        if (
                            isinstance(step.output, dict)
                            and step.output.get("command_id") == receipt.command_id
                        ):
                            self.failure += f" · 策略第 {step.line} 行 ({step.name})"
                            break
            self._snapshot(policy.snapshot)
            for step in policy.steps:
                if step.kind == "stop" and isinstance(step.output, dict):
                    self.stop = (
                        "已确认 ACK"
                        if step.output.get("acknowledged") is True
                        else "未确认"
                    )
            if policy.report and policy.task_outcome == "passed":
                self._report(policy.report)
            elif policy.report:
                self._report(policy.report)
                if policy.task_outcome != "passed" and policy.report.status == "passed":
                    self.task = "验证已过期，需重新验证"
        if output.report:
            self._report(output.report)

    def _snapshot(self, snapshot: HardwareSnapshotOutput | None) -> None:
        if snapshot is None:
            return
        self.device = snapshot.device_id
        uri = snapshot.state.get("recording_uri")
        if isinstance(uri, str):
            self.artifacts["video"] = uri

    def _receipt(
        self, receipt: HardwareCommandReceiptOutput | HardwareStopReceiptOutput
    ) -> None:
        if isinstance(receipt, HardwareStopReceiptOutput):
            self.stop = "已确认 ACK" if receipt.acknowledged else "未确认"
            return
        self.task = "动作后待重新验证"
        self.stop = "未确认"
        motion = receipt.result.get("motion")
        if isinstance(motion, dict):
            reached = motion.get("reached") is True
            status = "已到位" if reached else "未到位"
            reason = motion.get("reason", "unknown")
            error = motion.get("error_max_m")
            error_text = (
                f"{error * 1000:.1f} mm" if isinstance(error, (int, float)) else "未知"
            )
            self.control = f"{status} · {reason} · 误差 {error_text} · {motion.get('cycles', '?')} 周期"
            if not reached:
                self.failure = f"移动未到位：{reason}（{receipt.command_id}）"
            uri = motion.get("trace_uri")
            if isinstance(uri, str):
                self.artifacts["trace"] = uri
        else:
            self.control = f"动作回执 {receipt.status}（未提供到位判定）"
        if receipt.status != "completed":
            self.failure = (
                receipt.error or f"动作 {receipt.status}：{receipt.command_id}"
            )

    def _report(self, report: HardwareVerificationOutput) -> None:
        self.task = f"{report.criterion}：{report.status}"
        checks = "\n".join(
            f"{check.name}: {check.status} · {check.observed}"
            for check in report.checks
        )
        self.verification = f"{report.summary}\n{checks}"
        if report.status != "passed":
            self.failure = f"独立验证未通过：{report.criterion} · {report.summary}"

    def summary(self) -> str:
        return (
            f"Robo · {self.device or '设备发现'} · {self.phase}\n"
            f"控制：{self.control}\n任务：{self.task}\n"
            f"自主收尾（工具回执）：停止 {self.stop} · 租约 {self.lease}"
        )


def _live_phase(message: str) -> str:
    # These are presentation labels for the existing text stream, not inferred
    # approach/lift intent or additional device state.
    labels = {
        "device_observed": "观察已更新",
        "command_completed": "动作回执已返回，继续执行策略",
        "command_failed": "动作失败",
        "command_cancelled": "动作已取消",
        "task_verification_completed": "独立验证已返回",
        "stop_requested": "正在停止，等待 ACK",
        "stop_acknowledged": "停止已确认 ACK",
    }
    for kind, label in labels.items():
        if kind in message:
            return label
    if "command_started" in message:
        action = message.rsplit(" · ", 1)[-1]
        label = {
            "move_to": "移动到目标",
            "move_cartesian": "增量移动",
            "gripper_close": "闭爪抓取（尚未验证抓住）",
            "gripper_open": "打开夹爪",
            "home": "复位",
        }.get(action, action)
        return f"正在执行：{label} ({action})"
    return f"执行中：{message}"


def local_artifact_path(uri: str) -> Path | None:
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    path = Path(unquote(parsed.path))
    if not path.is_absolute() or path.suffix.lower() not in {
        ".mp4",
        ".py",
        ".json",
        ".jsonl",
    }:
        return None
    return path


class HardwareProgressPanel(Vertical):
    """Fixed above the input; does not disappear when tool groups collapse."""

    DEFAULT_CSS = """
    HardwareProgressPanel { height: auto; max-height: 13; border-top: solid $primary; padding: 0 1; }
    HardwareProgressPanel .hardware-summary { height: auto; max-height: 6; }
    HardwareProgressPanel .hardware-details { height: auto; max-height: 5; overflow-y: auto; }
    HardwareProgressPanel Horizontal { height: 1; }
    HardwareProgressPanel Button { height: 1; min-width: 12; border: none; margin-right: 1; padding: 0 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.progress = HardwareProgress()
        self.turn_id: str | None = None
        self.turn_error = ""
        self.cleanup: HardwareCleanupResult | None = None
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", classes="hardware-summary", markup=False)
        with Horizontal():
            yield Button("验证 / 失败详情", id="hardware-details", compact=True)
            yield Button("打开录像", id="hardware-video", disabled=True, compact=True)
            yield Button("策略源码", id="hardware-source", disabled=True, compact=True)
            yield Button(
                "最近运动轨迹", id="hardware-trace", disabled=True, compact=True
            )
        yield Static("", classes="hardware-details", markup=False)

    def reset(self, turn_id: str | None = None) -> None:
        self.progress = HardwareProgress()
        self.turn_id = turn_id
        self.turn_error = ""
        self.cleanup = None
        self.display = False
        if self.is_mounted:
            self.query_one(".hardware-details", Static).display = False

    def handle_event(self, event: AppServerEvent) -> None:
        if isinstance(event, TurnStarted):
            self.reset(event.turn.id)
        elif isinstance(event, SessionSnapshot):
            self.reset(event.state.latest_turn.id if event.state.latest_turn else None)
            for entry in event.state.history or []:
                if (
                    isinstance(entry, PublicEffectEntry)
                    and entry.turn_id == self.turn_id
                ):
                    self._entry(entry)
            if event.state.latest_turn:
                self._finish(event.state.latest_turn)
        elif isinstance(event, HistoryEntryAdded | HistoryEntryUpdated):
            if isinstance(event.entry, PublicEffectEntry):
                self._entry(event.entry)
            elif isinstance(event.entry, PublicReasoningEntry) or (
                isinstance(event.entry, PublicMessageEntry)
                and event.entry.role == "assistant"
            ):
                if not any(
                    effect.state.status in {"running", "pending", "blocked"}
                    for effect in self.progress.effects.values()
                ):
                    self.progress.phase = "模型正在生成下一步"
        elif isinstance(event, TurnCompleted) and event.turn.id == self.turn_id:
            self._finish(event.turn)
        self._refresh_summary()

    def _entry(self, entry: PublicEffectEntry) -> None:
        if not isinstance(entry.detail, HardwareEffectDetail):
            return
        if self.turn_id != entry.turn_id:
            self.reset(entry.turn_id)
        self.progress.update(entry)
        self.display = True

    def _finish(self, turn: PublicTurn) -> None:
        status = turn.status
        self.turn_error = turn.error.message if turn.error else ""
        self.cleanup = turn.hardware_cleanup
        if self.cleanup and (self.cleanup.devices or self.cleanup.error):
            self.display = True
        if status is not PublicTurnStatus.IN_PROGRESS:
            self.progress.phase = {
                PublicTurnStatus.COMPLETED: "本轮已结束（结果见下方）",
                PublicTurnStatus.INTERRUPTED: "本轮已中断",
                PublicTurnStatus.FAILED: "本轮失败",
            }.get(status, str(status))

    def _cleanup_text(self) -> tuple[str, str]:
        if self.cleanup is None:
            return "", ""
        lines: list[str] = []
        reports: list[str] = []
        for device in self.cleanup.devices:
            stop = (
                "已确认 ACK"
                if device.stop and device.stop.acknowledged
                else "未确认 ACK"
            )
            release = {
                "released": "已释放",
                "no_lease": "无租约，无需释放",
                "unconfirmed": "未确认释放",
            }[device.release]
            lines.append(f"系统兜底 · {device.device_id}：停止 {stop} · 租约 {release}")
            if report := device.verification:
                reports.append(
                    f"兜底独立验证 · {device.device_id}：{report.criterion}：{report.status}\n"
                    f"{report.summary}\n"
                    + "\n".join(
                        f"{check.name}: {check.status} · {check.observed}"
                        for check in report.checks
                    )
                )
            reports.extend(
                f"兜底失败 · {device.device_id} · {operation}：{error}"
                for operation, error in device.errors.items()
            )
        if self.cleanup.error:
            reports.append(f"系统兜底未完成：{self.cleanup.error}")
        summary = (
            lines[0]
            if len(lines) == 1
            else (f"系统兜底：{len(lines)} 台设备，见详情" if lines else "")
        )
        if self.cleanup.error:
            summary = "系统兜底未完成，见详情"
        return summary, "\n".join([*lines, *reports])

    def _refresh_summary(self) -> None:
        if not self.is_mounted:
            return
        cleanup_summary, cleanup_detail = self._cleanup_text()
        summary = self.progress.summary()
        if cleanup_summary:
            summary += f"\n{cleanup_summary}"
        self.query_one(".hardware-summary", Static).update(Content(summary))
        failures = []
        if self.turn_error:
            failures.append(f"本轮错误：{self.turn_error}")
        if self.progress.failure:
            failures.append(f"工具失败：{self.progress.failure}")
        detail = "\n".join(failures or ["暂无已记录失败"])
        detail += f"\n自主验证：{self.progress.verification or '尚无独立验证报告'}"
        if cleanup_detail:
            detail += f"\n{cleanup_detail}"
        self.query_one(".hardware-details", Static).update(Content(detail))
        for name in ("video", "source", "trace"):
            uri = self.progress.artifacts.get(name, "")
            self.query_one(f"#hardware-{name}", Button).disabled = (
                local_artifact_path(uri) is None
            )

    def on_mount(self) -> None:
        self.query_one(".hardware-details", Static).display = False
        self._refresh_summary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        name = (event.button.id or "").removeprefix("hardware-")
        if name == "details":
            details = self.query_one(".hardware-details", Static)
            details.display = not details.display
            return
        if uri := self.progress.artifacts.get(name):
            self.run_worker(self._open_artifact(uri), exclusive=True)

    async def _open_artifact(self, uri: str) -> None:
        path = local_artifact_path(uri)
        if path is None or not await asyncio.to_thread(path.is_file):
            self.notify(
                "文件尚未生成或不在本机。录像可能仍在写入。", severity="warning"
            )
            return
        if sys.platform == "darwin":
            arguments = (
                ["open", str(path)]
                if path.suffix.lower() == ".mp4"
                else ["open", "-t", str(path)]
            )
        else:
            arguments = [
                "xdg-open",
                str(path if path.suffix.lower() == ".mp4" else path.parent),
            ]
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
            if process.returncode:
                self.notify(f"无法打开：{path}", severity="warning")
        except OSError:
            self.notify(f"无法打开：{path}", severity="warning")
