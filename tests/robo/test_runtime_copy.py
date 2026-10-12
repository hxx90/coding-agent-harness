from __future__ import annotations

import copy
import threading
import zipfile
from pathlib import Path

import pytest

from agent_harness.robo import programs
from agent_harness.robo.demo import MANIFEST
from agent_harness.robo.store import Store


def test_copy_runtime_files_preserves_bytes_and_independent_destinations(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(bytes(range(256)) * 100)
    copies = [(source, tmp_path / "target" / f"nested-{i}" / "file") for i in range(20)]
    programs.copy_runtime_files(copies)
    assert all(
        destination.read_bytes() == source.read_bytes() for _, destination in copies
    )
    source.write_bytes(b"changed ambient source")
    assert all(
        destination.read_bytes() == bytes(range(256)) * 100 for _, destination in copies
    )


def test_copy_runtime_files_is_parallel_but_bounded(tmp_path, monkeypatch):
    gate = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    seen = set()
    active = peak = 0

    def copy_file(source, destination):
        nonlocal active, peak
        assert destination.parent.is_dir()
        with lock:
            active += 1
            peak = max(peak, active)
            seen.add(source)
        try:
            gate.wait()
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(programs.shutil, "copyfile", copy_file)
    files = [(Path(f"source-{i}"), tmp_path / "target" / str(i)) for i in range(16)]
    programs.copy_runtime_files(files)
    assert len(seen) == 16
    assert 2 <= peak <= 8


def test_empty_copy_does_not_create_a_pool(monkeypatch):
    def unexpected_pool(**kwargs):
        pytest.fail("No threads needed for an empty runtime file set")

    monkeypatch.setattr(programs, "ThreadPoolExecutor", unexpected_pool)
    programs.copy_runtime_files([])


def test_copy_error_is_propagated_after_workers_stop(tmp_path, monkeypatch):
    gate = threading.Barrier(2, timeout=5)
    finished = threading.Event()

    def copy_file(source, destination):
        gate.wait()
        if source.name == "bad":
            raise OSError("injected copy error")
        destination.write_bytes(b"complete")
        finished.set()

    monkeypatch.setattr(programs.shutil, "copyfile", copy_file)
    with pytest.raises(OSError, match="injected copy error"):
        programs.copy_runtime_files(
            [
                (Path("bad"), tmp_path / "a" / "bad"),
                (Path("good"), tmp_path / "b" / "good"),
            ]
        )
    assert finished.is_set()


def test_failed_copy_never_publishes_runtime_or_program(tmp_path, monkeypatch):
    def fail(files):
        assert files
        raise OSError("injected copy error")

    monkeypatch.setattr(programs, "copy_runtime_files", fail)
    store = Store(tmp_path)
    registry = programs.Programs(store)
    try:
        with pytest.raises(OSError, match="injected copy error"):
            registry.publish(
                {
                    "manifest": copy.deepcopy(MANIFEST),
                    "sources": {"control.py": "pass\n"},
                }
            )
        assert store.all("program") == []
        assert registry.runtimes == {}
        assert not (tmp_path / "programs").exists()
        assert all(
            path.name.endswith(".tmp") for path in (tmp_path / "runtimes").iterdir()
        )
    finally:
        store.db.close()


def test_program_is_registered_only_after_its_archive_is_complete(
    tmp_path, monkeypatch
):
    store = Store(tmp_path)
    registry = programs.Programs(store)
    original_put = store.put
    monkeypatch.setattr(
        registry, "_runtime", lambda dependencies: {"id": "test-runtime"}
    )

    def put(kind, key, value, **kwargs):
        if kind == "program":
            with zipfile.ZipFile(tmp_path / "programs" / (key + ".zip")) as archive:
                assert archive.read("control.py") == b"pass\n"
        return original_put(kind, key, value, **kwargs)

    monkeypatch.setattr(store, "put", put)
    try:
        version = registry.publish(
            {
                "manifest": copy.deepcopy(MANIFEST),
                "sources": {"control.py": "pass\n"},
            }
        )
        assert store.get("program", version["id"])["sources"] == {
            "control.py": "pass\n"
        }
    finally:
        store.db.close()


def test_interrupted_archive_can_be_republished_without_reusing_partial_bytes(
    tmp_path, monkeypatch
):
    store = Store(tmp_path)
    registry = programs.Programs(store)
    monkeypatch.setattr(
        registry, "_runtime", lambda dependencies: {"id": "test-runtime"}
    )
    original_zip = zipfile.ZipFile

    class InterruptedZip(original_zip):
        def writestr(self, name, data, *args, **kwargs):
            super().writestr(name, "partial", *args, **kwargs)
            raise OSError("interrupted program archive")

    bundle = {
        "manifest": copy.deepcopy(MANIFEST),
        "sources": {"control.py": "pass\n"},
    }
    try:
        monkeypatch.setattr(programs.zipfile, "ZipFile", InterruptedZip)
        with pytest.raises(OSError, match="interrupted program archive"):
            registry.publish(bundle)
        assert store.all("program") == []
        assert not list((tmp_path / "programs").glob("*.zip"))
        monkeypatch.setattr(programs.zipfile, "ZipFile", original_zip)
        version = registry.publish(bundle)
        with original_zip(tmp_path / "programs" / (version["id"] + ".zip")) as archive:
            assert archive.read("control.py") == b"pass\n"
    finally:
        store.db.close()
