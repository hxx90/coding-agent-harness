from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
import sys
from typing import ClassVar
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from vibe.app_server.models import HardwareDeviceOutput
from vibe.app_server.protocol import HardwareSetupCancelResponse, HardwareSetupResponse


def robot_label(device: HardwareDeviceOutput) -> str:
    physical = device.metadata.get("is_physical")
    kind = (
        "真实设备"
        if physical is True
        else (
            "模拟设备"
            if physical is False or device.transport == "simulator"
            else "设备类型未声明"
        )
    )
    return f"{device.model} · {device.device_id} · {kind}"


class RobotScreen(ModalScreen[None]):
    """Idle-only operator setup via public server APIs; never executes policies."""

    BINDINGS: ClassVar = [Binding("escape", "close", "关闭")]
    DEFAULT_CSS = """
    RobotScreen { align: center middle; background: $background 85%; }
    #robot-content { width: 92%; max-width: 110; height: auto; max-height: 90%; border: solid $primary; padding: 1 2; }
    #robot-content Static { height: auto; margin-bottom: 1; }
    #robot-content Select { margin-bottom: 1; }
    #robot-content Horizontal { height: auto; }
    #robot-content Button { width: 1fr; min-width: 0; margin-right: 1; padding: 0 1; }
    #robot-close { margin-right: 0; }
    """

    def __init__(
        self,
        setup: Callable[..., Awaitable[HardwareSetupResponse]],
        *,
        cancel: Callable[[str], Awaitable[HardwareSetupCancelResponse]] | None = None,
    ) -> None:
        super().__init__(id="robot-setup")
        self._setup = setup
        self._cancel = cancel
        self._operation_id: str | None = None
        self._load_task: asyncio.Task[object] | None = None
        self._closing = False
        self._response = HardwareSetupResponse()
        self._busy = False
        self._image_uri: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="robot-content"):
            yield Static("选择机器人 → 连接 → 查看能力与当前观测", markup=False)
            yield Static("检查模型配置与机器人连接…", id="robot-status", markup=False)
            yield Select([], prompt="选择机器人", id="robot-device")
            yield Static("", id="robot-capabilities", markup=False)
            yield Static("尚未读取观测。", id="robot-observation", markup=False)
            with Horizontal():
                yield Button("刷新列表", id="robot-refresh")
                yield Button(
                    "连接并读取", id="robot-connect", variant="primary", disabled=True
                )
                yield Button("打开画面", id="robot-image", disabled=True)
                yield Button("返回任务", id="robot-close")
            yield Static(
                "连接不会自动取得租约、武装或运动。选择会用于之后的自然语言任务。",
                markup=False,
            )

    async def on_mount(self) -> None:
        self.query_one("#robot-content").border_title = "Robo · 机器人"
        self.run_worker(self._load())

    async def _load(self, *, connect: bool = False) -> None:
        if self._busy:
            return
        self._load_task = asyncio.current_task()
        self._operation_id = uuid4().hex
        selected = self.query_one("#robot-device", Select).value
        device_id = selected if isinstance(selected, str) else None
        self._busy = True
        for button in self.query(Button):
            button.disabled = button.id != "robot-close"
        self.query_one("#robot-close", Button).label = "取消并返回"
        self.query_one(Select).disabled = True
        self.query_one("#robot-status", Static).update(
            "正在连接并读取观测…" if connect else "正在检查模型配置并发现机器人…"
        )
        try:
            self._response = await self._setup(
                device_id,
                connect=connect,
                **({"operation_id": self._operation_id} if self._cancel else {}),
            )
            picker = self.query_one("#robot-device", Select)
            picker.set_options([
                (robot_label(d), d.device_id) for d in self._response.devices
            ])
            ids = {d.device_id for d in self._response.devices}
            choice = device_id or self._response.selected_device_id
            if choice in ids:
                picker.value = choice
            model_status = (
                "配置可用，未发请求验证连通性"
                if self._response.model_configured
                else "配置待修复"
            )
            status = f"模型：{self._response.model_name or '未选择'} · {model_status}"
            if self._response.selected_device_id:
                label = (
                    "当前任务机器人"
                    if self._response.selection_confirmed
                    else "上次选择（本次未确认）"
                )
                status += f"\n{label}：{self._response.selected_device_id}"
            if not ids:
                status += "\n未发现机器人。请在 /mcp 中检查设备连接配置。"
            if self._response.issues:
                status += "\n" + "\n".join(self._response.issues)
            self.query_one("#robot-status", Static).update(status)
            self._show_observation()
        except Exception as exc:
            self._response = HardwareSetupResponse()
            self.query_one("#robot-status", Static).update(
                f"机器人设置不可用（{type(exc).__name__}）。请检查连接，或使用 --legacy-harness 启动。"
            )
            self._show_observation()
        finally:
            self._busy = False
            if self.is_mounted and self.query("#robot-close"):
                self.query_one("#robot-close", Button).label = "返回任务"
                self.query_one(Select).disabled = False
                self.query_one("#robot-refresh", Button).disabled = False
                self.query_one("#robot-close", Button).disabled = False
                self._show_capabilities()
                self._show_observation()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "robot-device":
            self._show_capabilities()
            self._show_observation()

    def _show_capabilities(self) -> None:
        selected = self.query_one(Select).value
        device = next(
            (d for d in self._response.devices if d.device_id == selected), None
        )
        self.query_one("#robot-connect", Button).disabled = self._busy or device is None
        if device is None:
            self.query_one("#robot-capabilities", Static).update(
                "选择设备后查看它声明的能力。"
            )
            return
        env = device.metadata.get("environment", "未声明；以当前观测为准")
        viewer = device.metadata.get("has_viewer")
        recording = device.metadata.get("records_video")
        state = lambda value: (
            "已配置开启"
            if value is True
            else ("未开启" if value is False else "未声明")
        )
        text = (
            f"{robot_label(device)}\n当前环境：{env}\n"
            f"可用动作：{', '.join(a.name for a in device.actions) or '无'}\n"
            f"独立验证：{', '.join(v.name for v in device.verifications) or '未声明'}\n"
            f"Viewer：{state(viewer)} · 录像：{state(recording)}"
        )
        self.query_one("#robot-capabilities", Static).update(text)

    def _show_observation(self) -> None:
        snapshot = self._response.snapshot
        self._image_uri = None
        text = "尚未读取所选机器人的观测。"
        if snapshot and snapshot.device_id == self.query_one(Select).value:
            text = f"最新观测：{snapshot.observed_at or '时间未声明'} · 序号 {snapshot.sequence}\n设备状态：{snapshot.status}"
            position = snapshot.state.get("end_effector_position")
            if isinstance(position, list):
                text += f"\n末端位置（机器人自身状态）：{position}"
            for evidence in snapshot.evidence:
                if evidence.modality == "rgb" and self._local_image(evidence.uri):
                    self._image_uri = evidence.uri
                    break
            if self._image_uri is None:
                text += "\n设备未提供可在本机打开的 RGB 画面。"
        self.query_one("#robot-observation", Static).update(text)
        self.query_one("#robot-image", Button).disabled = (
            self._busy or self._image_uri is None
        )

    @staticmethod
    def _local_image(uri: str) -> Path | None:
        parsed = urlsplit(uri)
        path = Path(unquote(parsed.path))
        if (
            parsed.scheme == "file"
            and parsed.netloc in {"", "localhost"}
            and path.is_absolute()
            and path.suffix.lower() in {".png", ".jpg", ".jpeg"}
        ):
            return path
        return None

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "robot-refresh":
            self.run_worker(self._load())
        elif event.button.id == "robot-connect":
            self.run_worker(self._load(connect=True))
        elif event.button.id == "robot-close":
            self.action_close()
        elif event.button.id == "robot-image" and self._image_uri:
            path = self._local_image(self._image_uri)
            if path is None or not await asyncio.to_thread(path.is_file):
                self.notify("画面文件不存在或不在本机。", severity="warning")
                return
            try:
                process = await asyncio.create_subprocess_exec(
                    "open" if sys.platform == "darwin" else "xdg-open",
                    str(path),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await process.wait()
                if process.returncode:
                    self.notify("无法打开当前画面。", severity="warning")
            except OSError:
                self.notify("无法打开当前画面。", severity="warning")

    def action_close(self) -> None:
        if self._closing:
            return
        if not self._busy:
            self.dismiss(None)
            return
        self._closing = True
        self.run_worker(self._cancel_and_close())

    async def _cancel_and_close(self) -> None:
        try:
            self.query_one("#robot-status", Static).update(
                "正在取消，等待后台连接检查结束…"
            )
            task = self._load_task
            if self._cancel and self._operation_id:
                async with asyncio.timeout(6):
                    result = await self._cancel(self._operation_id)
                if not result.settled and task is not None and not task.done():
                    raise RuntimeError("Cancellation not acknowledged")
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self.dismiss(None)
        except Exception as exc:
            self.query_one("#robot-status", Static).update(
                f"取消尚未确认（{type(exc).__name__}），请重试；后台结束前不会显示已取消。"
            )
        finally:
            self._closing = False
