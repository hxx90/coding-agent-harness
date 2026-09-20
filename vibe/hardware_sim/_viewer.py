from __future__ import annotations

import argparse
from collections.abc import Callable
import mmap
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO, Protocol
from uuid import uuid4

_MAGIC = b"ROBOST01"
_HEADER = struct.Struct("<8sIIIQ")
_FLOAT64_BYTES = 8
_MOCAP_VALUES = 7


class _Process(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


type ViewerLauncher = Callable[[Path, Path, int], _Process]
type ModelSaver = Callable[[Any, Path], None]


class _StateStorage:
    def __init__(self, *, path: Path, nq: int, nmocap: int) -> None:
        self.path = path
        self.nq = nq
        self.nmocap = nmocap
        self.value_count = nq + nmocap * _MOCAP_VALUES
        self.payload_size = self.value_count * _FLOAT64_BYTES
        self._sequence = 0
        self._stream: BinaryIO = path.open("w+b")
        self._stream.truncate(_HEADER.size + self.payload_size)
        self._mapping = mmap.mmap(
            self._stream.fileno(),
            _HEADER.size + self.payload_size,
            access=mmap.ACCESS_WRITE,
        )

    def write(self, data: Any) -> None:
        payload = _state_payload(data, nq=self.nq, nmocap=self.nmocap)
        self._sequence += 2
        self._write_header(self._sequence - 1)
        self._mapping[_HEADER.size :] = payload
        self._write_header(self._sequence)

    def close(self) -> None:
        self._mapping.close()
        self._stream.close()

    def _write_header(self, sequence: int) -> None:
        self._mapping[: _HEADER.size] = _HEADER.pack(
            _MAGIC, self.nq, self.nmocap, self.value_count, sequence
        )


class ViewerProcess:
    """Mirror simulator state into an isolated, display-only MuJoCo process."""

    def __init__(
        self,
        *,
        launcher: ViewerLauncher | None = None,
        model_saver: ModelSaver | None = None,
        temp_parent: Path | None = None,
    ) -> None:
        self._launcher = launcher or _launch_viewer
        self._model_saver = model_saver or _save_model
        self._temp_parent = temp_parent
        self._temporary_directory: Path | None = None
        self._storage: _StateStorage | None = None
        self._process: _Process | None = None
        self._model_identity: int | None = None
        self._dismissed = False

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def publish(self, model: Any, data: Any) -> None:
        nq, nmocap = _model_dimensions(model)
        if self._dismissed:
            return
        if self._process is not None and self._process.poll() is not None:
            self._dismissed = True
            self._release_storage()
            self._process = None
            return
        model_changed = self._model_identity not in {None, id(model)}
        dimensions_changed = self._storage is not None and (
            self._storage.nq,
            self._storage.nmocap,
        ) != (nq, nmocap)
        if model_changed or dimensions_changed:
            self.close()
            self._dismissed = False
        if self._storage is None:
            self._start(model, data, nq=nq, nmocap=nmocap)
            return
        self._storage.write(data)

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except (subprocess.TimeoutExpired, TimeoutError):
                process.kill()
                try:
                    process.wait(timeout=2)
                except (subprocess.TimeoutExpired, TimeoutError):
                    pass
        self._release_storage()
        self._model_identity = None
        self._dismissed = True

    def _start(self, model: Any, data: Any, *, nq: int, nmocap: int) -> None:
        temporary_directory = self._create_temporary_directory()
        model_path = temporary_directory / "scene.mjb"
        storage = _StateStorage(
            path=temporary_directory / "latest-state.bin", nq=nq, nmocap=nmocap
        )
        try:
            self._model_saver(model, model_path)
            storage.write(data)
            process = self._launcher(model_path, storage.path, os.getpid())
        except BaseException:
            storage.close()
            shutil.rmtree(temporary_directory, ignore_errors=True)
            raise
        self._temporary_directory = temporary_directory
        self._storage = storage
        self._process = process
        self._model_identity = id(model)

    def _release_storage(self) -> None:
        if self._storage is not None:
            self._storage.close()
            self._storage = None
        if self._temporary_directory is not None:
            shutil.rmtree(self._temporary_directory, ignore_errors=True)
            self._temporary_directory = None

    def _create_temporary_directory(self) -> Path:
        if self._temp_parent is None:
            return Path(tempfile.mkdtemp(prefix="robo-viewer-"))
        directory = self._temp_parent / f"robo-viewer-{uuid4().hex}"
        directory.mkdir(parents=True)
        return directory


def read_stable_state(path: Path) -> tuple[Any, Any, Any]:
    """Read qpos, mocap positions, and mocap quaternions from a state buffer."""
    with path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
            current = _read_stable_state_or_none(mapping)
    if current is None:
        raise ValueError("Viewer state buffer has no stable state")
    _, qpos, mocap_pos, mocap_quat = current
    return qpos, mocap_pos, mocap_quat


def _read_stable_state_or_none(mapping: mmap.mmap) -> tuple[int, Any, Any, Any] | None:
    import numpy as np

    first = _unpack_header(mapping)
    if first is None:
        return None
    nq, nmocap, value_count, sequence = first
    if sequence % 2 != 0:
        return None
    payload_size = value_count * _FLOAT64_BYTES
    payload = bytes(mapping[_HEADER.size : _HEADER.size + payload_size])
    if _unpack_header(mapping) != first:
        return None
    values = np.frombuffer(payload, dtype=np.float64).copy()
    qpos = values[:nq]
    mocap_values = values[nq:]
    mocap_pos = mocap_values[: nmocap * 3].reshape(nmocap, 3)
    mocap_quat = mocap_values[nmocap * 3 :].reshape(nmocap, 4)
    return sequence, qpos, mocap_pos, mocap_quat


def _unpack_header(mapping: mmap.mmap) -> tuple[int, int, int, int] | None:
    if len(mapping) < _HEADER.size:
        return None
    magic, nq, nmocap, value_count, sequence = _HEADER.unpack(mapping[: _HEADER.size])
    expected_count = nq + nmocap * _MOCAP_VALUES
    expected_size = _HEADER.size + expected_count * _FLOAT64_BYTES
    if magic != _MAGIC or value_count != expected_count or expected_size > len(mapping):
        return None
    return nq, nmocap, value_count, sequence


def _model_dimensions(model: Any) -> tuple[int, int]:
    nq = getattr(model, "nq", None)
    nmocap = getattr(model, "nmocap", None)
    if not isinstance(nq, int) or nq <= 0:
        raise ValueError("Viewer model must declare a positive nq")
    if not isinstance(nmocap, int) or nmocap < 0:
        raise ValueError("Viewer model must declare a non-negative nmocap")
    return nq, nmocap


def _state_payload(data: Any, *, nq: int, nmocap: int) -> bytes:
    import numpy as np

    qpos = np.asarray(getattr(data, "qpos", ()), dtype=np.float64).reshape(-1)
    mocap_pos = np.asarray(getattr(data, "mocap_pos", ()), dtype=np.float64).reshape(-1)
    mocap_quat = np.asarray(getattr(data, "mocap_quat", ()), dtype=np.float64).reshape(
        -1
    )
    if qpos.size != nq or mocap_pos.size != nmocap * 3 or mocap_quat.size != nmocap * 4:
        raise ValueError("Viewer state dimensions do not match the model")
    return np.concatenate((qpos, mocap_pos, mocap_quat)).tobytes(order="C")


def _save_model(model: Any, path: Path) -> None:
    import mujoco

    mujoco_module: Any = mujoco
    raw_model = getattr(model, "_model", model)
    mujoco_module.mj_saveModel(raw_model, str(path))


def _launch_viewer(model_path: Path, state_path: Path, parent_pid: int) -> _Process:
    return subprocess.Popen(
        [
            str(_viewer_python()),
            str(Path(__file__).resolve()),
            "--model",
            str(model_path),
            "--state-buffer",
            str(state_path),
            "--parent-pid",
            str(parent_pid),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )


def _viewer_python() -> Path:
    executable = Path(sys.executable)
    if sys.platform != "darwin":
        return executable
    mjpython = executable.with_name("mjpython")
    if not mjpython.is_file():
        raise RuntimeError(
            "MuJoCo's mjpython launcher is missing; run `uv sync --extra simulator`"
        )
    return mjpython


def _parent_is_alive(parent_pid: int) -> bool:
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run_viewer(model_path: Path, state_path: Path, *, parent_pid: int) -> None:
    import mujoco
    from mujoco import viewer

    mujoco_module: Any = mujoco
    model = mujoco_module.MjModel.from_binary_path(str(model_path))
    data = mujoco_module.MjData(model)
    with state_path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
            with viewer.launch_passive(
                model, data, show_left_ui=False, show_right_ui=False
            ) as handle:
                # Robosuite keeps collision geoms in group 0 and polished visual
                # geoms in group 1. Native MuJoCo enables both by default.
                handle.opt.geomgroup[0] = 0
                handle.opt.geomgroup[1] = 1
                handle.cam.lookat[:] = [0.0, 0.0, 0.9]
                handle.cam.distance = 2.4
                handle.cam.azimuth = 135.0
                handle.cam.elevation = -20.0
                _viewer_loop(
                    mujoco_module, handle, model, data, mapping, parent_pid=parent_pid
                )


def _viewer_loop(
    mujoco: Any,
    handle: Any,
    model: Any,
    data: Any,
    mapping: mmap.mmap,
    *,
    parent_pid: int,
) -> None:
    last_sequence = -1
    while handle.is_running() and _parent_is_alive(parent_pid):
        current = _read_stable_state_or_none(mapping)
        if current is not None and current[0] != last_sequence:
            last_sequence, qpos, mocap_pos, mocap_quat = current
            with handle.lock():
                data.qpos[:] = qpos
                if model.nmocap:
                    data.mocap_pos[:] = mocap_pos
                    data.mocap_quat[:] = mocap_quat
                mujoco.mj_forward(model, data)
            handle.sync()
        time.sleep(0.015)


def main() -> None:
    parser = argparse.ArgumentParser(description="Display a Robo MuJoCo state mirror")
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--state-buffer", required=True, type=Path)
    parser.add_argument("--parent-pid", required=True, type=int)
    args = parser.parse_args()
    _run_viewer(args.model, args.state_buffer, parent_pid=args.parent_pid)


if __name__ == "__main__":
    main()


__all__ = ["ViewerProcess", "read_stable_state"]
