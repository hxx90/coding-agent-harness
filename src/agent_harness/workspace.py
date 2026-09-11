"""Safe text-file operations rooted in a configured Workspace."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from .config import WorkspaceConfig
from .errors import ToolExecutionError, WorkspaceError


MAX_FILE_BYTES = 1024 * 1024
MAX_TOOL_OUTPUT_CHARS = 200 * 1024
IGNORED_NAMES = {
    ".git",
    ".venv",
    ".tox",
    ".nox",
    ".harness-tools",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
}
MAX_ISOLATED_FILES = 5_000
MAX_ISOLATED_BYTES = 100 * 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_utf8(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise WorkspaceError(
            "file_not_found",
            "无法读取文件：%s" % path,
            recoverable=True,
        ) from exc
    if size > max_bytes:
        raise WorkspaceError(
            "unsupported_file",
            "文件超过 MVP 的 1 MB 上限：%s" % path.name,
            recoverable=False,
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError(
            "unsupported_file",
            "仅支持 UTF-8 文本文件：%s" % path.name,
            recoverable=False,
        ) from exc


def atomic_write_text(path: Path, content: str, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".harness-write-", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _directory_is_sensitive(config: WorkspaceConfig, path: Path) -> bool:
    try:
        relative = path.resolve(strict=False).relative_to(config.root).as_posix()
    except ValueError:
        return True
    return bool(relative and not config.can_read(relative))


def iter_visible_files(
    config: WorkspaceConfig,
    start: Optional[Path] = None,
) -> Iterator[Tuple[str, Path]]:
    base = start or config.root
    for current_root, directories, files in os.walk(base, followlinks=False):
        root_path = Path(current_root)
        kept_directories: List[str] = []
        for name in sorted(directories):
            candidate = root_path / name
            if name in IGNORED_NAMES or candidate.is_symlink():
                continue
            if _directory_is_sensitive(config, candidate):
                continue
            kept_directories.append(name)
        directories[:] = kept_directories

        for name in sorted(files):
            candidate = root_path / name
            if candidate.is_symlink():
                continue
            relative = candidate.relative_to(config.root).as_posix()
            if not config.can_read(relative):
                continue
            yield relative, candidate


def safe_file_text(config: WorkspaceConfig, relative_path: str) -> str:
    path = config.resolve_relative(relative_path)
    relative = path.relative_to(config.root).as_posix()
    config.assert_readable(relative)
    if not path.is_file():
        raise WorkspaceError(
            "file_not_found",
            "不是可读取文件：%s" % relative_path,
            recoverable=True,
        )
    return read_utf8(path)


def create_isolated_workspace(config: WorkspaceConfig) -> Path:
    """Copy visible project files into a disposable autonomous workspace."""
    destination_parent = Path(tempfile.mkdtemp(prefix="harness-autonomous-"))
    destination = destination_parent / config.root.name
    destination.mkdir(parents=True, exist_ok=False)
    copied_files = 0
    copied_bytes = 0
    try:
        for relative, source in iter_visible_files(config):
            if relative == ".agent-harness.json":
                # The config is copied like every other visible file, but is
                # always read-only after loading the autonomous copy.
                pass
            copied_files += 1
            copied_bytes += source.stat().st_size
            if copied_files > MAX_ISOLATED_FILES:
                raise WorkspaceError(
                    "workspace_too_large",
                    "自主模式副本超过 %s 个文件上限" % MAX_ISOLATED_FILES,
                    recoverable=True,
                )
            if copied_bytes > MAX_ISOLATED_BYTES:
                raise WorkspaceError(
                    "workspace_too_large",
                    "自主模式副本超过 100 MB 上限",
                    recoverable=True,
                )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    except Exception:
        shutil.rmtree(destination_parent, ignore_errors=True)
        raise
    return destination
