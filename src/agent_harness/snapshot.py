"""Task-level snapshots, unified Diff generation, and safe revert."""

from __future__ import annotations

import difflib
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from .config import WorkspaceConfig
from .domain import RevertResult
from .errors import ToolExecutionError
from .workspace import atomic_write_text, iter_visible_files, read_utf8, sha256_file


class SnapshotManager:
    def __init__(self, config: WorkspaceConfig, task_dir: Path) -> None:
        self.config = config
        self.task_dir = task_dir
        self.snapshot_dir = task_dir / "snapshot"
        self.files_dir = self.snapshot_dir / "files"
        self.manifest_path = self.snapshot_dir / "manifest.json"
        self._lock = threading.RLock()
        self.original: Dict[str, Dict[str, Any]] = {}
        self.current_hashes: Dict[str, Optional[str]] = {}
        self.touched_files: Set[str] = set()

    def create(self) -> None:
        with self._lock:
            self.files_dir.mkdir(parents=True, exist_ok=True)
            for relative, path in iter_visible_files(self.config):
                if not self.config.can_write(relative):
                    continue
                try:
                    content = read_utf8(path)
                except Exception:
                    continue
                destination = self.files_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
                mode = path.stat().st_mode & 0o777
                digest = sha256_file(path)
                self.original[relative] = {"sha256": digest, "mode": mode}
                self.current_hashes[relative] = digest
            self._write_manifest()

    def _write_manifest(self) -> None:
        payload = {
            "files": self.original,
            "current_hashes": self.current_hashes,
            "touched_files": sorted(self.touched_files),
        }
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def expected_hash(self, relative_path: str) -> Optional[str]:
        return self.current_hashes.get(relative_path)

    def assert_current(self, relative_path: str) -> None:
        with self._lock:
            path = self.config.root / relative_path
            expected = self.current_hashes.get(relative_path)
            if path.exists():
                actual = sha256_file(path)
                if expected is None or actual != expected:
                    raise ToolExecutionError(
                        "concurrent_modification",
                        "文件在任务期间被外部修改：%s" % relative_path,
                        recoverable=True,
                    )
            elif expected is not None:
                raise ToolExecutionError(
                    "concurrent_modification",
                    "文件在任务期间被外部删除：%s" % relative_path,
                    recoverable=True,
                )

    def mark_changed(self, relative_paths: Iterable[str]) -> None:
        with self._lock:
            for relative in relative_paths:
                path = self.config.root / relative
                self.current_hashes[relative] = sha256_file(path) if path.exists() else None
                self.touched_files.add(relative)
            self._write_manifest()

    def assert_touched_current(self) -> None:
        """Fail if a managed file changed outside the Agent after its last patch."""
        with self._lock:
            for relative in sorted(self.touched_files):
                self.assert_current(relative)

    def original_text(self, relative_path: str) -> Optional[str]:
        if relative_path not in self.original:
            return None
        return (self.files_dir / relative_path).read_text(encoding="utf-8")

    def diff(self) -> str:
        sections: List[str] = []
        with self._lock:
            for relative in sorted(self.touched_files):
                before = self.original_text(relative) or ""
                path = self.config.root / relative
                after = path.read_text(encoding="utf-8") if path.exists() else ""
                section = difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile="a/%s" % relative,
                    tofile="b/%s" % relative,
                )
                sections.extend(section)
        return "".join(sections)

    def changed_files(self) -> List[str]:
        return sorted(self.touched_files)

    def revert(self) -> RevertResult:
        result = RevertResult(ok=True)
        with self._lock:
            for relative in sorted(self.touched_files):
                path = self.config.root / relative
                expected = self.current_hashes.get(relative)
                try:
                    if path.exists():
                        actual = sha256_file(path)
                        if expected is None or actual != expected:
                            result.conflicted_files.append(relative)
                            result.ok = False
                            continue
                    elif expected is not None:
                        result.conflicted_files.append(relative)
                        result.ok = False
                        continue

                    original = self.original_text(relative)
                    if original is None:
                        if path.exists():
                            path.unlink()
                        result.deleted_files.append(relative)
                    else:
                        mode = int(self.original[relative]["mode"])
                        atomic_write_text(path, original, mode)
                        result.restored_files.append(relative)
                except Exception as exc:
                    result.ok = False
                    result.errors[relative] = str(exc)

            if result.ok:
                self.touched_files.clear()
                self.current_hashes = {
                    relative: metadata["sha256"]
                    for relative, metadata in self.original.items()
                }
            self._write_manifest()
        return result
