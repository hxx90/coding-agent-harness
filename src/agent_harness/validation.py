"""Run the single configured validation command safely."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Event
from typing import Dict, Optional, Tuple

from .config import WorkspaceConfig
from .domain import ValidationResult
from .javascript import check_javascript_syntax, check_website_runtime
from .trace import Redactor, SENSITIVE_NAME_RE
from .sandbox import python_launcher_rules


OUTPUT_LIMIT = 200 * 1024


def _truncate(value: str) -> Tuple[str, bool]:
    if len(value) <= OUTPUT_LIMIT:
        return value, False
    half = OUTPUT_LIMIT // 2
    marker = "\n...[TRUNCATED BY VALIDATION RUNNER]...\n"
    return value[:half] + marker + value[-half:], True


class ValidationRunner:
    def __init__(
        self,
        config: WorkspaceConfig,
        redactor: Optional[Redactor] = None,
        *,
        sandboxed: bool = False,
        scratch_dir: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.redactor = redactor or Redactor()
        self.sandboxed = sandboxed
        self.scratch_dir = scratch_dir.resolve() if scratch_dir else None
        if self.sandboxed and self.scratch_dir is None:
            raise ValueError("sandboxed validation requires scratch_dir")
        if self.scratch_dir is not None:
            self.scratch_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.scratch_dir, 0o700)
            except OSError:
                pass

    def _environment(self, run_scratch: Optional[Path] = None) -> Dict[str, str]:
        if self.sandboxed:
            environment = {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
            }
            if run_scratch is not None:
                environment.update(
                    {
                        "TMPDIR": str(run_scratch),
                        "TMP": str(run_scratch),
                        "TEMP": str(run_scratch),
                    }
                )
            for name in ("LANG", "LC_ALL", "LC_CTYPE", "TZ"):
                value = os.environ.get(name)
                if value:
                    environment[name] = value
            return environment
        environment: Dict[str, str] = {}
        for name, value in os.environ.items():
            if SENSITIVE_NAME_RE.search(name):
                continue
            if name.startswith("AGENT_HARNESS_"):
                continue
            environment[name] = value
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment

    @staticmethod
    def _sandbox_literal(path: Path) -> str:
        escaped = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        return '"%s"' % escaped

    def _command(
        self, run_scratch: Optional[Path] = None
    ) -> Tuple[Optional[list[str]], Optional[str]]:
        command = list(self.config.validation_command)
        if not self.sandboxed:
            return command, None
        sandbox_executable = shutil.which("sandbox-exec")
        if sys.platform != "darwin" or not sandbox_executable:
            return None, "自主模式要求 macOS sandbox-exec，当前环境不可用"
        executable = Path(command[0])
        if not executable.is_absolute():
            resolved = shutil.which(command[0])
            if not resolved:
                return None, "找不到验证命令：%s" % command[0]
            executable = Path(resolved)
            command[0] = str(executable)
        executable_paths = {executable.resolve(), executable}
        readable_roots = {
            self.config.root.resolve(),
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
            *(path.parent.parent for path in executable_paths),
        }
        process_rules = " ".join(
            "(literal %s)" % self._sandbox_literal(path)
            for path in sorted(executable_paths, key=lambda item: str(item))
        )
        read_rules = " ".join(
            "(subpath %s)" % self._sandbox_literal(path)
            for path in sorted(readable_roots, key=lambda item: str(item))
        )
        metadata_paths = set()
        for root in (*readable_roots, *(path for path in (run_scratch,) if path)):
            metadata_paths.update((root, *root.parents))
        metadata_rules = " ".join(
            "(literal %s)" % self._sandbox_literal(path)
            for path in sorted(metadata_paths, key=lambda item: str(item))
        )
        sensitive_denials = "".join(
            "(deny file-read* (subpath %s))" % self._sandbox_literal(
                self.config.root / relative
            )
            for relative in self.config.sensitive_paths
        )
        profile = (
            '(version 1)(deny default)(import "system.sb")'
            "(allow process-exec %s)(allow process-info*)"
            "(allow file-read* %s)"
            "(allow file-read-metadata %s)%s%s"
            "(deny network*)(deny file-write*)%s"
        ) % (
            process_rules,
            read_rules,
            metadata_rules,
            python_launcher_rules(executable),
            sensitive_denials,
            (
                "(allow file-write* (subpath %s))"
                % self._sandbox_literal(run_scratch)
                if run_scratch is not None
                else ""
            ),
        )
        return [sandbox_executable, "-p", profile, *command], None

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except Exception:
                pass

    def _website_javascript_path(self) -> Optional[Path]:
        requirements_path = self.config.root / "site-requirements.json"
        app_path = self.config.root / "app.js"
        if not requirements_path.is_file() or not app_path.is_file():
            return None
        try:
            requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(requirements, dict):
            return None
        if requirements.get("project_type") != "static_website":
            return None
        return app_path

    def _javascript_preflight(self, started: float) -> Optional[ValidationResult]:
        app_path = self._website_javascript_path()
        if app_path is None:
            return None
        result = check_javascript_syntax(app_path, self.config.root)
        if not (result.get("available") and result.get("passed")):
            diagnostics = str(result.get("diagnostics") or result.get("message") or "")
            diagnostics = self.redactor.text(diagnostics)
            diagnostics, truncated = _truncate(diagnostics)
            return ValidationResult(
                exit_code=(1 if result.get("available") else None),
                timed_out=bool(result.get("timed_out")),
                cancelled=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout="JavaScript syntax check failed\n" + diagnostics,
                stderr="",
                truncated=truncated,
                runner_error=(None if result.get("available") else diagnostics),
            )

        manifest_path = self.config.root / "game-manifest.json"
        if not manifest_path.is_file():
            return None
        runtime = check_website_runtime(app_path, manifest_path, self.config.root)
        if runtime.get("available") and runtime.get("passed"):
            return None
        diagnostics = str(runtime.get("diagnostics") or runtime.get("message") or "")
        diagnostics = self.redactor.text(diagnostics)
        diagnostics, truncated = _truncate(diagnostics)
        return ValidationResult(
            exit_code=(1 if runtime.get("available") else None),
            timed_out=bool(runtime.get("timed_out")),
            cancelled=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout="Website runtime smoke failed\n" + diagnostics,
            stderr="",
            truncated=truncated,
            runner_error=(None if runtime.get("available") else diagnostics),
        )

    def run(self, stop_event: Optional[Event] = None) -> ValidationResult:
        stop = stop_event or Event()
        started = time.monotonic()
        if stop.is_set():
            return ValidationResult(
                exit_code=None,
                timed_out=False,
                cancelled=True,
                duration_ms=0,
                stdout="",
                stderr="",
            )
        javascript_failure = self._javascript_preflight(started)
        if javascript_failure is not None:
            return javascript_failure
        run_scratch = (
            Path(tempfile.mkdtemp(prefix="run-", dir=str(self.scratch_dir)))
            if self.sandboxed and self.scratch_dir is not None
            else None
        )
        command, command_error = self._command(run_scratch)
        if command_error or command is None:
            return ValidationResult(
                exit_code=None,
                timed_out=False,
                cancelled=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout="",
                stderr="",
                runner_error=command_error or "无法构建验证命令",
            )
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.config.root),
                env=self._environment(run_scratch),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            return ValidationResult(
                exit_code=None,
                timed_out=False,
                cancelled=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout="",
                stderr="",
                runner_error=str(exc),
            )

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
                if time.monotonic() - started >= self.config.validation_timeout_seconds:
                    timed_out = True
                    self._terminate(process)
                    stdout, stderr = process.communicate()
                    break

        stdout = self.redactor.text(stdout or "")
        stderr = self.redactor.text(stderr or "")
        stdout, stdout_truncated = _truncate(stdout)
        stderr, stderr_truncated = _truncate(stderr)
        return ValidationResult(
            exit_code=process.returncode,
            timed_out=timed_out,
            cancelled=cancelled,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_truncated or stderr_truncated,
        )
