"""Real project files, optimistic edits, reversible changes, and bounded commands."""

from __future__ import annotations

import base64
import difflib
import fnmatch
import hashlib
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from agent_harness.patching import apply_file_patch, parse_unified_diff
from agent_harness.trace import SENSITIVE_NAME_RE
from agent_harness.workspace import atomic_write_text

from .settings import Settings
from .types import Cancelled, CodingError, Json

MAX_FILE_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024
MAX_SNAPSHOT_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 5000
MAX_OUTPUT = 64 * 1024
IGNORED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".DS_Store",
}
CONTROL_FILES = {".agent-harness.json", ".agent-harness.toml"}


def matches(path: str, pattern: str) -> bool:
    return (
        path == pattern
        or path.startswith(pattern.rstrip("/") + "/")
        or fnmatch.fnmatchcase(path, pattern)
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Workspace:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.project
        self.observed: dict[str, str | None] = {}
        self.extra_paths: set[str] = set()
        self.before_write: Callable[[str, Json | None], None] | None = None

    def path(self, name: str, *, writable: bool = False) -> Path:
        if not isinstance(name, str) or not name or "\x00" in name:
            raise CodingError("invalid_path", "A non-empty project path is required")
        candidate = Path(name)
        if ".." in candidate.parts:
            raise CodingError("path_outside_project", "Parent traversal is not allowed")
        if candidate.is_absolute():
            try:
                candidate = candidate.relative_to(self.root)
            except ValueError as exc:
                raise CodingError(
                    "path_outside_project", f"Path is outside project: {name}"
                ) from exc
        current = self.root
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise CodingError("symlink", f"Symbolic links are not followed: {name}")
        if current != self.root and self.root not in current.resolve().parents:
            raise CodingError(
                "path_outside_project", f"Path is outside project: {name}"
            )
        relative = current.relative_to(self.root).as_posix()
        parts = candidate.parts
        if any(
            part in {".git", ".venv", "venv", "node_modules"}
            or part == ".env"
            or part.startswith(".env.")
            for part in parts
        ):
            raise CodingError(
                "sensitive_path",
                f"Sensitive or dependency path is not available: {relative}",
            )
        if any(matches(relative, rule) for rule in self.settings.sensitive):
            raise CodingError(
                "sensitive_path", f"Sensitive path is not available: {relative}"
            )
        if writable:
            if (
                relative == "."
                or relative in CONTROL_FILES
                or any(matches(relative, rule) for rule in self.settings.protected)
            ):
                raise CodingError(
                    "protected_path", f"Protected path cannot be edited: {relative}"
                )
            if self.settings.editable is not None and not any(
                matches(relative, rule) for rule in self.settings.editable
            ):
                raise CodingError(
                    "protected_path",
                    f"Path is outside configured editable paths: {relative}",
                )
        return current

    def _bytes(self, path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
        if not path.is_file():
            raise CodingError(
                "file_not_found", f"Not a file: {path.relative_to(self.root)}"
            )
        if path.stat().st_size > limit:
            raise CodingError(
                "file_too_large",
                f"File exceeds {limit} bytes: {path.relative_to(self.root)}",
            )
        return path.read_bytes()

    def text(self, name: str) -> str:
        path = self.path(name)
        data = self._bytes(path)
        try:
            text = data.decode("utf-8")
            if "\x00" in text:
                raise UnicodeError("binary content")
        except UnicodeError as exc:
            raise CodingError(
                "binary_file", f"Only UTF-8 text is supported: {name}"
            ) from exc
        self.observed[path.relative_to(self.root).as_posix()] = _digest(data)
        return text

    def read(self, name: str, offset: int = 1, limit: int = 200) -> Json:
        if (
            isinstance(offset, bool)
            or isinstance(limit, bool)
            or offset < 1
            or not 1 <= limit <= 2000
        ):
            raise CodingError(
                "invalid_arguments", "offset must be >=1 and limit must be 1..2000"
            )
        lines = self.text(name).splitlines()
        selected = lines[offset - 1 : offset - 1 + limit]
        content = "\n".join(
            f"{offset + index}: {line}" for index, line in enumerate(selected)
        )
        return {
            "path": name,
            "content": content[:MAX_OUTPUT],
            "total_lines": len(lines),
            "truncated": len(content) > MAX_OUTPUT or offset - 1 + limit < len(lines),
            "instructions": self.instructions(name),
        }

    def files(self, pattern: str = "**/*") -> list[str]:
        files: set[str] = set()
        git_listing = False
        # Git's own ignore semantics preserve tracked files and exclude generated files.
        if (self.root / ".git").exists() and shutil.which("git"):
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.root),
                        "ls-files",
                        "-z",
                        "--cached",
                        "--others",
                        "--exclude-standard",
                    ],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0:
                    git_listing = True
                    files.update(
                        item.decode("utf-8", errors="replace")
                        for item in result.stdout.split(b"\0")
                        if item
                    )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not git_listing:
            for parent, dirs, names in os.walk(self.root, followlinks=False):
                dirs[:] = [
                    name
                    for name in dirs
                    if name not in IGNORED and not (Path(parent) / name).is_symlink()
                ]
                for name in names:
                    if name not in IGNORED:
                        files.add(
                            (Path(parent) / name).relative_to(self.root).as_posix()
                        )
                if len(files) > MAX_FILES:
                    raise CodingError(
                        "project_too_large",
                        f"Project exceeds {MAX_FILES} visible files",
                    )
        files.update(self.extra_paths)
        result_paths = []
        for relative in sorted(files):
            try:
                path = self.path(relative)
                if path.is_file() and (
                    fnmatch.fnmatchcase(relative, pattern)
                    or (
                        pattern.startswith("**/")
                        and fnmatch.fnmatchcase(relative, pattern[3:])
                    )
                ):
                    result_paths.append(relative)
            except CodingError:
                continue
        if len(result_paths) > MAX_FILES:
            raise CodingError(
                "project_too_large", f"Project exceeds {MAX_FILES} visible files"
            )
        return result_paths

    def grep(self, query: str, pattern: str = "**/*", limit: int = 100) -> Json:
        if not query or len(query) > 1000 or not 1 <= limit <= 1000:
            raise CodingError(
                "invalid_arguments", "Use a non-empty search string and limit 1..1000"
            )
        found = []
        for name in self.files(pattern):
            try:
                data = self._bytes(self.path(name)).decode("utf-8")
            except (CodingError, UnicodeError, OSError):
                continue
            for line_number, line in enumerate(data.splitlines(), 1):
                if query in line:
                    found.append(
                        {"path": name, "line": line_number, "text": line[:1000]}
                    )
                    if len(found) >= limit:
                        return {"matches": found, "truncated": True}
        return {"matches": found, "truncated": False}

    def _assert_current(self, path: Path) -> None:
        name = path.relative_to(self.root).as_posix()
        if path.exists() and name not in self.observed:
            raise CodingError(
                "file_not_read", f"Read {name} before changing an existing file"
            )
        actual = _digest(self._bytes(path)) if path.exists() else None
        if name in self.observed and self.observed[name] != actual:
            raise CodingError(
                "external_modification",
                f"{name} changed since it was last read; read it again",
            )

    def write(self, name: str, content: str) -> Json:
        path = self.path(name, writable=True)
        if (
            not isinstance(content, str)
            or len(content.encode("utf-8")) > MAX_FILE_BYTES
            or "\x00" in content
        ):
            raise CodingError(
                "invalid_arguments", "content must be UTF-8 text of at most 1 MiB"
            )
        self._assert_current(path)
        before = path.read_bytes().decode("utf-8") if path.exists() else None
        if before == content:
            raise CodingError(
                "no_changes", "New content is identical to the current file"
            )
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        if self.before_write:
            self.before_write(
                path.relative_to(self.root).as_posix(), self.snapshot_file(name)
            )
        atomic_write_text(path, content, mode)
        relative = path.relative_to(self.root).as_posix()
        self.observed[relative] = _digest(path.read_bytes())
        self.extra_paths.add(relative)
        return {
            "path": relative,
            "bytes": path.stat().st_size,
            "changed": True,
            "instructions": self.instructions(name),
        }

    def edit(self, name: str, old: str, new: str, replace_all: bool = False) -> Json:
        path = self.path(name, writable=True)
        self._assert_current(path)
        source = self._bytes(path).decode("utf-8")
        if not old:
            raise CodingError("invalid_arguments", "old_string cannot be empty")
        count = source.count(old)
        if count == 0:
            raise CodingError(
                "edit_not_found", "old_string was not found; read the current file"
            )
        if count > 1 and not replace_all:
            raise CodingError(
                "edit_ambiguous",
                f"old_string matches {count} locations; add context or set replace_all",
            )
        return self.write(name, source.replace(old, new, -1 if replace_all else 1))

    def patch(self, patch: str) -> Json:
        prepared = []
        names: set[str] = set()
        try:
            for file in parse_unified_diff(patch):
                if file.path in names:
                    raise CodingError(
                        "patch_invalid", "A file can appear only once in a patch"
                    )
                names.add(file.path)
                path = self.path(file.path, writable=True)
                self._assert_current(path)
                if file.is_new == path.exists():
                    raise CodingError(
                        "patch_conflict",
                        f"Unexpected existing/missing file: {file.path}",
                    )
                before = path.read_text(encoding="utf-8") if path.exists() else ""
                after = apply_file_patch(before, file)
                if len(after.encode("utf-8")) > MAX_FILE_BYTES:
                    raise CodingError("file_too_large", "Patched file exceeds 1 MiB")
                prepared.append((file.path, after))
        except CodingError:
            raise
        except Exception as exc:
            raise CodingError("patch_invalid", str(exc)) from exc
        if not prepared:
            raise CodingError("patch_invalid", "No changes found in unified diff")
        saved = {name: self.snapshot_file(name) for name, _ in prepared}
        try:
            for name, content in prepared:
                self.write(name, content)
        except BaseException:
            for name, original in saved.items():
                self._restore(name, original)
            raise
        return {"changed": True, "paths": [name for name, _ in prepared]}

    def snapshot_file(self, name: str) -> Json | None:
        path = self.path(name)
        if not path.exists():
            return None
        data = self._bytes(path, MAX_SNAPSHOT_FILE_BYTES)
        return {
            "data": base64.b64encode(data).decode("ascii"),
            "mode": path.stat().st_mode & 0o777,
        }

    def capture(self) -> Json:
        snapshot: Json = {}
        size = 0
        for name in self.files():
            path = self.path(name)
            if path.stat().st_size > MAX_SNAPSHOT_FILE_BYTES:
                continue
            size += path.stat().st_size
            if size > MAX_SNAPSHOT_BYTES:
                raise CodingError(
                    "project_too_large", "Reversible project snapshot exceeds 100 MiB"
                )
            snapshot[name] = self.snapshot_file(name)
        return snapshot

    def changes(self, before: Json) -> Json:
        after = self.capture()
        return {
            name: {"before": before.get(name), "after": after.get(name)}
            for name in sorted(before.keys() | after.keys())
            if before.get(name) != after.get(name)
        }

    @staticmethod
    def diff(changes: Json) -> str:
        sections: list[str] = []
        for name, versions in sorted(changes.items()):
            before = versions.get("before")
            after = versions.get("after")
            old = (
                base64.b64decode(before["data"]).decode("utf-8", errors="replace")
                if before
                else ""
            )
            new = (
                base64.b64decode(after["data"]).decode("utf-8", errors="replace")
                if after
                else ""
            )
            if before is None:
                sections.append(
                    f"diff --git a/{name} b/{name}\nnew file mode {after['mode']:06o}\n"
                )
            elif after is None:
                sections.append(
                    f"diff --git a/{name} b/{name}\ndeleted file mode {before['mode']:06o}\n"
                )
            elif before["mode"] != after["mode"]:
                sections.append(
                    f"diff --git a/{name} b/{name}\nold mode {before['mode']:06o}\nnew mode {after['mode']:06o}\n"
                )
            for line in difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile="a/" + name,
                tofile="b/" + name,
            ):
                sections.append(
                    line
                    if line.endswith("\n")
                    else line + "\n\\ No newline at end of file\n"
                )
        return "".join(sections)

    def _restore(self, name: str, snapshot: Json | None) -> None:
        path = self.path(name, writable=True)
        if snapshot is None:
            path.unlink(missing_ok=True)
            self.observed[name] = None
            return
        data = base64.b64decode(snapshot["data"], validate=True)
        if len(data) > MAX_SNAPSHOT_FILE_BYTES:
            raise CodingError(
                "file_too_large", "Snapshot file exceeds restoration limit"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(".harness-restore-" + str(time.time_ns()))
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, int(snapshot["mode"]) & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self.observed[name] = _digest(data)

    def restore(self, changes: Json, *, undo: bool) -> None:
        expected, target = ("after", "before") if undo else ("before", "after")
        for name, versions in changes.items():
            self.path(name, writable=True)
            if self.snapshot_file(name) != versions.get(expected):
                raise CodingError(
                    "undo_conflict",
                    f"{name} changed outside this session; no files were restored",
                )
        done = []
        try:
            for name, versions in changes.items():
                self._restore(name, versions.get(target))
                done.append(name)
        except BaseException:
            for name in reversed(done):
                self._restore(name, changes[name].get(expected))
            raise

    def instructions(self, name: str = ".") -> list[Json]:
        target = self.path(name)
        directory = target if target.is_dir() else target.parent
        directories = [self.root]
        if directory != self.root:
            directories.extend(
                reversed(
                    [directory, *directory.parents][
                        : len(directory.relative_to(self.root).parts)
                    ]
                )
            )
        result = []
        for directory in directories:
            source = directory / "AGENTS.md"
            if source.is_file() and not source.is_symlink():
                try:
                    safe_source = self.path(source.relative_to(self.root).as_posix())
                    data = self._bytes(safe_source, 32 * 1024).decode("utf-8")
                    result.append(
                        {
                            "path": source.relative_to(self.root).as_posix(),
                            "content": data,
                        }
                    )
                except (CodingError, UnicodeError):
                    continue
        return result


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    finally:
        # The shell may have exited while a descendant still holds stdout open.
        # Reap that process group even when waiting for the parent already succeeded.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass


def command_environment(secrets: tuple[str, ...] = ()) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not SENSITIVE_NAME_RE.search(key)
        and not any(secret and secret in value for secret in secrets)
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_command(
    command: list[str],
    cwd: Path,
    *,
    timeout: float,
    stop: threading.Event,
    secrets: tuple[str, ...] = (),
) -> Json:
    if stop.is_set():
        raise Cancelled()
    environment = command_environment(secrets)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise CodingError(
            "command_start_failed", f"Cannot start command: {exc}"
        ) from exc
    output: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=64)
    readers_done = threading.Event()

    def enqueue(item: tuple[str, bytes | None]) -> None:
        while not readers_done.is_set():
            try:
                output.put(item, timeout=0.05)
                return
            except queue.Full:
                continue

    def read(pipe: BinaryIO, name: str) -> None:
        try:
            while chunk := pipe.read(4096):
                enqueue((name, chunk))
        finally:
            enqueue((name, None))
            pipe.close()

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=read, args=(pipe, name), daemon=True)
        for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr"))
    ]
    for reader in readers:
        reader.start()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    closed = 0
    truncated = False
    timed_out = False
    cancelled = False
    try:
        while closed < 2 or process.poll() is None:
            if stop.is_set() or time.monotonic() - started >= timeout:
                cancelled = stop.is_set()
                timed_out = not cancelled
                terminate_process(process)
            if closed == 2:
                time.sleep(0.05)
                continue
            try:
                name, chunk = output.get(timeout=0.05)
            except queue.Empty:
                continue
            if chunk is None:
                closed += 1
            else:
                remaining = MAX_OUTPUT - len(buffers[name])
                buffers[name].extend(chunk[: max(0, remaining)])
                truncated = truncated or len(chunk) > remaining
        process.wait()
    except BaseException:
        terminate_process(process)
        raise
    finally:
        readers_done.set()
        for reader in readers:
            reader.join(timeout=0.5)
    return {
        "exit_code": process.returncode,
        "stdout": buffers["stdout"].decode("utf-8", errors="replace"),
        "stderr": buffers["stderr"].decode("utf-8", errors="replace"),
        "timed_out": timed_out,
        "cancelled": cancelled,
        "truncated": truncated,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
