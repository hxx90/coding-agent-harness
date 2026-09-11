"""Ephemeral Python helper tools for autonomous mode."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from typing import Any, Dict, List, Optional, Tuple

from .config import WorkspaceConfig
from .errors import ToolExecutionError
from .trace import Redactor, SENSITIVE_NAME_RE
from .workspace import atomic_write_text


MAX_SCRIPT_BYTES = 32 * 1024
MAX_SCRIPT_OUTPUT_CHARS = 64 * 1024
MAX_SCRIPT_ARGS = 20
MAX_SCRIPT_ARG_CHARS = 200
MAX_HELPER_SCRIPTS = 10
SCRIPT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}(?:\.py)?$")
WORKER_CODE = (
    "import resource,runpy,sys;"
    "limit=int(sys.argv[1]);script=sys.argv[2];args=sys.argv[3:];"
    "resource.setrlimit(resource.RLIMIT_CPU,(limit,limit));"
    "resource.setrlimit(resource.RLIMIT_FSIZE,(1048576,1048576));"
    "sys.argv=[script,*args];runpy.run_path(script,run_name='__main__')"
)


def _truncate(value: str) -> Tuple[str, bool]:
    if len(value) <= MAX_SCRIPT_OUTPUT_CHARS:
        return value, False
    half = MAX_SCRIPT_OUTPUT_CHARS // 2
    marker = "\n...[TRUNCATED BY HELPER SCRIPT RUNNER]...\n"
    return value[:half] + marker + value[-half:], True


def _sandbox_literal(value: Path) -> str:
    escaped = str(value.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % escaped


class HelperScriptRunner:
    """Write and run read-only helper scripts inside the macOS process sandbox."""

    def __init__(
        self,
        config: WorkspaceConfig,
        helper_dir: Path,
        redactor: Optional[Redactor] = None,
    ) -> None:
        self.config = config
        self.helper_dir = helper_dir.resolve()
        self.helper_dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        self.sandbox_executable = shutil.which("sandbox-exec")
        self.python_executable = Path(
            getattr(sys, "_base_executable", None) or sys.executable
        ).resolve()

    @property
    def available(self) -> bool:
        return bool(
            sys.platform == "darwin"
            and self.sandbox_executable
            and self.python_executable.is_file()
        )

    @staticmethod
    def normalize_name(name: Any) -> str:
        if not isinstance(name, str) or not SCRIPT_NAME_RE.fullmatch(name.strip()):
            raise ValueError(
                "name 必须由字母开头，只包含字母、数字、下划线或连字符"
            )
        normalized = name.strip()
        return normalized if normalized.endswith(".py") else normalized + ".py"

    def write(self, name: Any, source: Any) -> Dict[str, Any]:
        normalized = self.normalize_name(name)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source 必须是非空 Python 代码")
        encoded = source.encode("utf-8")
        if len(encoded) > MAX_SCRIPT_BYTES:
            raise ValueError("辅助脚本不能超过 32 KB")
        if "\x00" in source:
            raise ValueError("辅助脚本包含非法字符")
        try:
            ast.parse(source, filename=normalized)
        except SyntaxError as exc:
            raise ValueError("辅助脚本语法错误：%s" % exc) from exc
        path = self.helper_dir / normalized
        replaced = path.exists()
        evicted = None
        if not replaced:
            scripts = list(self.helper_dir.glob("*.py"))
            if len(scripts) >= MAX_HELPER_SCRIPTS:
                # Helper scripts are disposable analysis artifacts rather than
                # engineering output. Keep the storage bound without turning a
                # long-running Task into a permanent dead end after ten names.
                oldest = min(
                    scripts,
                    key=lambda item: (item.stat().st_mtime_ns, item.name),
                )
                oldest.unlink()
                evicted = oldest.name
        atomic_write_text(path, source, 0o600)
        return {
            "name": normalized,
            "size_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "replaced": replaced,
            "evicted": evicted,
        }

    def _profile(self) -> str:
        readable_roots = {
            self.config.root.resolve(),
            self.helper_dir,
            self.python_executable.parent.parent,
            Path(sys.base_prefix).resolve(),
        }
        allows = " ".join(
            "(subpath %s)" % _sandbox_literal(path)
            for path in sorted(readable_roots, key=lambda item: str(item))
        )
        denials = "".join(
            "(deny file-read* (subpath %s))" % _sandbox_literal(
                self.config.root / relative
            )
            for relative in self.config.sensitive_paths
        )
        return (
            '(version 1)(deny default)(import "system.sb")'
            "(allow process-exec (literal %s))(allow process-info*)"
            "(allow file-read* %s)%s"
            "(deny network*)(deny file-write*)"
        ) % (_sandbox_literal(self.python_executable), allows, denials)

    @staticmethod
    def _environment() -> Dict[str, str]:
        environment: Dict[str, str] = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }
        for name in ("LANG", "LC_ALL", "LC_CTYPE", "TZ"):
            value = os.environ.get(name)
            if value and not SENSITIVE_NAME_RE.search(name):
                environment[name] = value
        return environment

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1.0)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                pass

    def run(
        self,
        name: Any,
        args: Any,
        timeout_seconds: Any,
        stop_event: Optional[Event] = None,
    ) -> Dict[str, Any]:
        if not self.available:
            raise ToolExecutionError(
                "script_sandbox_unavailable",
                "当前系统没有可用的 macOS sandbox-exec，不能安全运行辅助脚本",
                recoverable=True,
            )
        normalized = self.normalize_name(name)
        path = self.helper_dir / normalized
        if not path.is_file():
            raise ToolExecutionError(
                "helper_script_not_found",
                "辅助脚本不存在：%s" % normalized,
                recoverable=True,
            )
        if args is None:
            args = []
        if (
            not isinstance(args, list)
            or len(args) > MAX_SCRIPT_ARGS
            or not all(
                isinstance(item, str) and len(item) <= MAX_SCRIPT_ARG_CHARS
                for item in args
            )
        ):
            raise ValueError("args 必须是最多 20 个、每项不超过 200 字符的字符串数组")
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            raise ValueError("timeout_seconds 必须是整数")
        timeout = max(1, min(30, timeout_seconds))
        command: List[str] = [
            str(self.sandbox_executable),
            "-p",
            self._profile(),
            str(self.python_executable),
            "-I",
            "-c",
            WORKER_CODE,
            str(timeout + 1),
            str(path),
            *args,
        ]
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.config.root),
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except OSError as exc:
            raise ToolExecutionError(
                "helper_script_start_failed",
                "无法启动辅助脚本：%s" % exc,
                recoverable=True,
            ) from exc

        stop = stop_event or Event()
        timed_out = False
        cancelled = False
        stdout = ""
        stderr = ""
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if stop.is_set():
                    cancelled = True
                    self._terminate(process)
                    stdout, stderr = process.communicate()
                    break
                if time.monotonic() - started >= timeout:
                    timed_out = True
                    self._terminate(process)
                    stdout, stderr = process.communicate()
                    break

        stdout = self.redactor.text(stdout or "")
        stderr = self.redactor.text(stderr or "")
        stdout, stdout_truncated = _truncate(stdout)
        stderr, stderr_truncated = _truncate(stderr)
        return {
            "name": normalized,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
            "sandbox": "macos-sandbox-exec-read-only",
        }
