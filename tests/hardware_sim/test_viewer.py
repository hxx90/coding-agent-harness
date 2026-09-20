from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vibe.hardware_sim._viewer import ViewerProcess, _unpack_header, read_stable_state


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            raise TimeoutError
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _model() -> Any:
    return SimpleNamespace(nq=2, nmocap=1)


def _data(np: Any, *, offset: float = 0.0) -> Any:
    return SimpleNamespace(
        qpos=np.array([1.0 + offset, 2.0 + offset]),
        mocap_pos=np.array([[3.0 + offset, 4.0 + offset, 5.0 + offset]]),
        mocap_quat=np.array([[1.0, 0.0 + offset, 0.1 + offset, 0.2 + offset]]),
    )


def _save_fake_model(_model: Any, path: Path) -> None:
    path.write_bytes(b"model")


def test_viewer_process_publishes_latest_simulator_state(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    launched: list[tuple[Path, Path, int]] = []
    process = FakeProcess()

    def launch(model_path: Path, state_path: Path, parent_pid: int) -> FakeProcess:
        launched.append((model_path, state_path, parent_pid))
        return process

    def save_model(model: Any, path: Path) -> None:
        assert model.nq == 2
        path.write_bytes(b"mujoco-model")

    model = _model()
    viewer = ViewerProcess(
        launcher=launch, model_saver=save_model, temp_parent=tmp_path
    )

    viewer.publish(model, _data(np))
    assert viewer.is_running is True
    assert len(launched) == 1
    model_path, state_path, _ = launched[0]
    assert model_path.read_bytes() == b"mujoco-model"
    qpos, mocap_pos, mocap_quat = read_stable_state(state_path)
    assert qpos.tolist() == [1.0, 2.0]
    assert mocap_pos.tolist() == [[3.0, 4.0, 5.0]]
    assert mocap_quat.tolist() == [[1.0, 0.0, 0.1, 0.2]]

    viewer.publish(model, _data(np, offset=10.0))
    assert len(launched) == 1
    qpos, mocap_pos, mocap_quat = read_stable_state(state_path)
    assert qpos.tolist() == [11.0, 12.0]
    assert mocap_pos.tolist() == [[13.0, 14.0, 15.0]]
    assert mocap_quat.tolist() == [[1.0, 10.0, 10.1, 10.2]]

    viewer.close()
    assert process.terminated is True
    assert state_path.exists() is False
    assert model_path.exists() is False


def test_closed_viewer_is_not_restarted_during_same_connection(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    processes: list[FakeProcess] = []

    def launch(*_: Any) -> FakeProcess:
        process = FakeProcess()
        processes.append(process)
        return process

    viewer = ViewerProcess(
        launcher=launch, model_saver=_save_fake_model, temp_parent=tmp_path
    )
    model = _model()
    viewer.publish(model, _data(np))
    processes[0].returncode = 0

    viewer.publish(model, _data(np, offset=1.0))

    assert viewer.is_running is False
    assert len(processes) == 1
    viewer.close()


def test_viewer_rejects_state_that_does_not_match_model(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    launched = False

    def launch(*_: Any) -> FakeProcess:
        nonlocal launched
        launched = True
        return FakeProcess()

    viewer = ViewerProcess(
        launcher=launch, model_saver=_save_fake_model, temp_parent=tmp_path
    )
    invalid_data = SimpleNamespace(
        qpos=np.zeros(3), mocap_pos=np.zeros((1, 3)), mocap_quat=np.zeros((1, 4))
    )

    with pytest.raises(ValueError, match="dimensions do not match"):
        viewer.publish(_model(), invalid_data)

    assert launched is False
    viewer.close()


def test_short_state_buffer_header_is_rejected(tmp_path: Path) -> None:
    import mmap

    path = tmp_path / "short-state.bin"
    path.write_bytes(b"short")

    with path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
            assert _unpack_header(mapping) is None
