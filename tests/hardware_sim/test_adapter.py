from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import platform
import struct
import sys
import time
from types import SimpleNamespace
from typing import Any
import zlib

import pytest

from vibe.core.hardware import HardwareCommand, HardwareRuntime
from vibe.hardware_sim import MUJOCO_PANDA_DEVICE_ID, RobosuiteSimulatorAdapter

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("robosuite") is None, reason="requires the simulator extra"
)


def _png_rows(contents: bytes) -> list[bytes]:
    offset = 8
    width = height = 0
    compressed = bytearray()
    while offset < len(contents):
        length = struct.unpack(">I", contents[offset : offset + 4])[0]
        kind = contents[offset + 4 : offset + 8]
        data = contents[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height = struct.unpack(">II", data[:8])
        elif kind == b"IDAT":
            compressed.extend(data)
    raw = zlib.decompress(compressed)
    stride = 1 + width * 3
    return [raw[row * stride + 1 : (row + 1) * stride] for row in range(height)]


def test_visible_simulator_uses_one_offscreen_camera_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class FakeEnvironment:
        def reset(self):
            return {"cube_pos": []}

    def make(**kwargs: object) -> FakeEnvironment:
        captured.update(kwargs)
        return FakeEnvironment()

    monkeypatch.setitem(sys.modules, "robosuite", SimpleNamespace(make=make))
    adapter = RobosuiteSimulatorAdapter(show_viewer=True, evidence_dir=tmp_path)

    adapter._ensure_environment()

    assert captured["has_renderer"] is False
    assert captured["has_offscreen_renderer"] is True
    assert captured["use_camera_obs"] is True


def test_native_viewer_does_not_require_an_offscreen_camera_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeEnvironment:
        def reset(self):
            return {"cube_pos": []}

    def make(**kwargs: object) -> FakeEnvironment:
        captured.update(kwargs)
        return FakeEnvironment()

    monkeypatch.setitem(sys.modules, "robosuite", SimpleNamespace(make=make))
    adapter = RobosuiteSimulatorAdapter(show_viewer=True)

    adapter._ensure_environment()

    assert captured["has_renderer"] is False
    assert captured["has_offscreen_renderer"] is False
    assert captured["use_camera_obs"] is False
    assert captured["camera_names"] == "sideview"


def test_visible_simulator_publishes_state_to_independent_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    np = pytest.importorskip("numpy")

    class FakeViewer:
        def __init__(self) -> None:
            self.states: list[tuple[Any, Any]] = []
            self.closed = False

        @property
        def is_running(self) -> bool:
            return not self.closed

        def publish(self, model: Any, data: Any) -> None:
            self.states.append((model, data))

        def close(self) -> None:
            self.closed = True

    viewer = FakeViewer()
    monkeypatch.setattr("vibe.hardware_sim.adapter.ViewerProcess", lambda: viewer)
    adapter = RobosuiteSimulatorAdapter(show_viewer=True)
    model = SimpleNamespace(nq=2, nmocap=0)
    data = SimpleNamespace(
        qpos=np.zeros(2), mocap_pos=np.empty((0, 3)), mocap_quat=np.empty((0, 4))
    )
    adapter._environment = SimpleNamespace(
        sim=SimpleNamespace(model=model, data=data), close=lambda: None
    )
    monkeypatch.setattr(adapter, "_is_grasped", lambda: False)

    adapter._render()

    assert viewer.states == [(model, data)]
    assert adapter._state("idle")["viewer"] is True

    adapter._close_environment()
    assert viewer.closed is True


def test_viewer_failure_does_not_fail_simulator_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class FailingViewer:
        @property
        def is_running(self) -> bool:
            return False

        def publish(self, model: Any, data: Any) -> None:
            del model, data
            raise RuntimeError("viewer unavailable")

        def close(self) -> None:
            return None

    def create_viewer() -> FailingViewer:
        nonlocal attempts
        attempts += 1
        return FailingViewer()

    monkeypatch.setattr("vibe.hardware_sim.adapter.ViewerProcess", create_viewer)
    adapter = RobosuiteSimulatorAdapter(show_viewer=True)
    adapter._environment = SimpleNamespace(
        sim=SimpleNamespace(model=object(), data=object())
    )

    adapter._render()
    adapter._render()

    assert attempts == 1
    assert adapter._viewer is None
    assert adapter._viewer_failed is True


def test_viewer_cleanup_failure_does_not_fail_simulator_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingViewer:
        @property
        def is_running(self) -> bool:
            return False

        def publish(self, model: Any, data: Any) -> None:
            del model, data
            raise RuntimeError("viewer unavailable")

        def close(self) -> None:
            raise RuntimeError("viewer cleanup unavailable")

    monkeypatch.setattr(
        "vibe.hardware_sim.adapter.ViewerProcess", lambda: FailingViewer()
    )
    adapter = RobosuiteSimulatorAdapter(show_viewer=True)
    adapter._environment = SimpleNamespace(
        sim=SimpleNamespace(model=object(), data=object())
    )

    adapter._render()

    assert adapter._viewer is None
    assert adapter._viewer_failed is True


def test_visual_evidence_is_saved_right_side_up(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    adapter = RobosuiteSimulatorAdapter(show_viewer=False, evidence_dir=tmp_path)
    adapter._observation = {
        "sideview_image": np.array(
            [[[255, 0, 0], [255, 0, 0]], [[0, 0, 255], [0, 0, 255]]], dtype=np.uint8
        )
    }

    [evidence] = adapter._capture_rgb_evidence()
    rows = _png_rows(Path(evidence.uri.removeprefix("file://")).read_bytes())

    assert rows[0] == bytes([0, 0, 255]) * 2
    assert rows[1] == bytes([255, 0, 0]) * 2


def test_simulator_records_upright_mp4_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    np = pytest.importorskip("numpy")

    class FakeVideoWriter:
        def __init__(self, path: str, *_: object) -> None:
            self.path = Path(path)
            self.frames: list[Any] = []
            self.released = False
            writers.append(self)

        def isOpened(self) -> bool:
            return True

        def write(self, frame: Any) -> None:
            self.frames.append(frame.copy())

        def release(self) -> None:
            self.released = True
            self.path.write_bytes(b"mp4")

    writers: list[FakeVideoWriter] = []

    fake_cv2 = SimpleNamespace(
        VideoWriter=FakeVideoWriter, VideoWriter_fourcc=lambda *_: 1
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    adapter = RobosuiteSimulatorAdapter(
        show_viewer=False, recording_dir=tmp_path / "recordings"
    )
    adapter._environment = object()
    adapter._observation = {
        "sideview_image": np.array([[[255, 0, 0]], [[0, 0, 255]]], dtype=np.uint8)
    }

    adapter._render()
    adapter._finish_recording()

    assert len(writers) == 1
    writer = writers[0]
    assert writer.released is True
    assert writer.path.suffix == ".mp4"
    assert writer.path.read_bytes() == b"mp4"
    # Camera rows are flipped upright, then RGB is converted to BGR for OpenCV.
    assert writer.frames[0].tolist() == [[[255, 0, 0]], [[0, 0, 255]]]


@pytest.mark.asyncio
async def test_runtime_controls_headless_mujoco_panda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_platform = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
    monkeypatch.setattr(sys, "platform", system_platform[platform.system()])
    adapter = RobosuiteSimulatorAdapter(show_viewer=False)
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)

    manifests = await runtime.discover()
    await runtime.connect(MUJOCO_PANDA_DEVICE_ID, run_id="mujoco-smoke")
    lease = await runtime.acquire_lease(
        MUJOCO_PANDA_DEVICE_ID, owner="test", run_id="mujoco-smoke"
    )
    await runtime.arm(
        MUJOCO_PANDA_DEVICE_ID, lease_id=lease.lease_id, run_id="mujoco-smoke"
    )
    before = await runtime.observe(MUJOCO_PANDA_DEVICE_ID, run_id="mujoco-smoke")
    receipt = await runtime.execute(
        HardwareCommand(
            device_id=MUJOCO_PANDA_DEVICE_ID,
            lease_id=lease.lease_id,
            action="move_cartesian",
            parameters={"delta_xyz": [0.2, 0.0, 0.0], "steps": 10},
        ),
        run_id="mujoco-smoke",
    )
    after = await runtime.observe(MUJOCO_PANDA_DEVICE_ID, run_id="mujoco-smoke")
    stopped = await runtime.stop(
        MUJOCO_PANDA_DEVICE_ID, reason="test complete", run_id="mujoco-smoke"
    )

    assert [manifest.device_id for manifest in manifests] == [MUJOCO_PANDA_DEVICE_ID]
    assert receipt.status == "completed"
    assert after.state["end_effector_position"] != before.state["end_effector_position"]
    assert stopped.acknowledged is True

    await runtime.aclose()


@pytest.mark.asyncio
async def test_completed_gripper_command_does_not_imply_task_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_platform = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
    monkeypatch.setattr(sys, "platform", system_platform[platform.system()])
    adapter = RobosuiteSimulatorAdapter(show_viewer=False)
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)

    await runtime.connect(MUJOCO_PANDA_DEVICE_ID, run_id="false-success")
    lease = await runtime.acquire_lease(
        MUJOCO_PANDA_DEVICE_ID, owner="test", run_id="false-success"
    )
    await runtime.arm(
        MUJOCO_PANDA_DEVICE_ID, lease_id=lease.lease_id, run_id="false-success"
    )
    receipt = await runtime.execute(
        HardwareCommand(
            device_id=MUJOCO_PANDA_DEVICE_ID,
            lease_id=lease.lease_id,
            action="gripper_close",
        ),
        run_id="false-success",
    )
    observed = await runtime.observe(MUJOCO_PANDA_DEVICE_ID, run_id="false-success")
    verification = await runtime.verify(
        MUJOCO_PANDA_DEVICE_ID, criterion="lift_object", run_id="false-success"
    )

    assert receipt.status == "completed"
    assert observed.state["grasped"] is False
    cube_height = observed.state["cube_height_above_table_m"]
    assert isinstance(cube_height, int | float)
    assert cube_height < 0.04
    distance = observed.state["gripper_to_cube_distance_m"]
    assert isinstance(distance, int | float)
    assert distance > 0.05
    assert "reward" not in observed.state
    assert "task_success" not in observed.state
    assert verification.status == "failed"
    assert await runtime.pending_verification_revisions() == {}
    assert {check.name: check.status for check in verification.checks} == {
        "object_grasped": "failed",
        "object_lifted": "failed",
    }

    await runtime.execute(
        HardwareCommand(
            device_id=MUJOCO_PANDA_DEVICE_ID,
            lease_id=lease.lease_id,
            action="move_cartesian",
            parameters={"delta_xyz": [0.0, 0.0, 0.01], "steps": 1},
        ),
        run_id="false-success",
    )
    assert set(await runtime.pending_verification_revisions()) == {
        MUJOCO_PANDA_DEVICE_ID
    }
    corrective_verification = await runtime.verify(
        MUJOCO_PANDA_DEVICE_ID, criterion="lift_object", run_id="false-success"
    )
    assert corrective_verification.status == "failed"
    assert await runtime.pending_verification_revisions() == {}

    await runtime.aclose()


@pytest.mark.asyncio
async def test_observe_captures_visual_evidence_in_the_trace_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_platform = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
    monkeypatch.setattr(sys, "platform", system_platform[platform.system()])
    evidence_dir = tmp_path / "evidence"
    adapter = RobosuiteSimulatorAdapter(show_viewer=False, evidence_dir=evidence_dir)
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)

    await runtime.connect(MUJOCO_PANDA_DEVICE_ID, run_id="visual-observation")
    observed = await runtime.observe(
        MUJOCO_PANDA_DEVICE_ID, run_id="visual-observation"
    )

    assert len(observed.evidence) == 1
    evidence = observed.evidence[0]
    assert evidence.modality == "rgb"
    assert evidence.source_id == "sideview"
    assert evidence.mime_type == "image/png"
    assert evidence.uri.startswith("file://")
    image_path = Path(evidence.uri.removeprefix("file://"))
    assert image_path.is_relative_to(evidence_dir)
    assert image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    await runtime.aclose()


@pytest.mark.asyncio
async def test_stop_interrupts_long_mujoco_motion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_platform = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
    monkeypatch.setattr(sys, "platform", system_platform[platform.system()])
    adapter = RobosuiteSimulatorAdapter(show_viewer=False)
    runtime = HardwareRuntime(adapters=[adapter], trace_dir=tmp_path)
    await runtime.connect(MUJOCO_PANDA_DEVICE_ID, run_id="mujoco-stop")
    lease = await runtime.acquire_lease(
        MUJOCO_PANDA_DEVICE_ID, owner="test", run_id="mujoco-stop"
    )
    await runtime.arm(
        MUJOCO_PANDA_DEVICE_ID, lease_id=lease.lease_id, run_id="mujoco-stop"
    )

    first_step = asyncio.Event()
    original_render = adapter._render

    def slow_render() -> None:
        original_render()
        first_step.set()
        time.sleep(0.01)

    monkeypatch.setattr(adapter, "_render", slow_render)
    execution = asyncio.create_task(
        runtime.execute(
            HardwareCommand(
                device_id=MUJOCO_PANDA_DEVICE_ID,
                lease_id=lease.lease_id,
                action="move_cartesian",
                parameters={"delta_xyz": [0.2, 0.0, 0.0], "steps": 200},
            ),
            run_id="mujoco-stop",
        )
    )

    await asyncio.wait_for(first_step.wait(), timeout=1)
    stopped = await runtime.stop(
        MUJOCO_PANDA_DEVICE_ID, reason="operator stop", run_id="mujoco-stop"
    )

    assert stopped.acknowledged is True
    with pytest.raises(asyncio.CancelledError):
        await execution
    await runtime.aclose()
