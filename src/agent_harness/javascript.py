"""Read-only JavaScript diagnostics for generated website tasks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


SYNTAX_OUTPUT_LIMIT = 4_000
RUNTIME_OUTPUT_LIMIT = 8_000


def check_javascript_syntax(
    path: Path,
    workspace_root: Path,
    *,
    timeout_seconds: int = 5,
) -> Dict[str, Any]:
    """Parse one JavaScript file with Node without executing project code."""
    node = shutil.which("node")
    if not node:
        return {
            "available": False,
            "passed": False,
            "message": "找不到 Node.js，无法执行 JavaScript 语法检查",
        }
    try:
        result = subprocess.run(
            [node, "--check", str(path.resolve())],
            cwd=str(workspace_root.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, min(timeout_seconds, 10)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "available": True,
            "passed": False,
            "timed_out": True,
            "message": "JavaScript 语法检查超时",
        }
    except OSError as exc:
        return {
            "available": False,
            "passed": False,
            "message": "无法启动 JavaScript 语法检查：%s" % exc,
        }

    output = "\n".join(
        value.strip() for value in (result.stdout, result.stderr) if value.strip()
    )
    output = output.replace(str(workspace_root.resolve()), ".")
    if len(output) > SYNTAX_OUTPUT_LIMIT:
        half = SYNTAX_OUTPUT_LIMIT // 2
        output = output[:half] + "\n...[TRUNCATED]...\n" + output[-half:]
    return {
        "available": True,
        "passed": result.returncode == 0,
        "exit_code": result.returncode,
        "diagnostics": output,
    }


def _sandbox_literal(path: Path) -> str:
    escaped = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % escaped


def _runtime_command(
    node: Path,
    smoke_script: Path,
    app_path: Path,
    manifest_path: Path,
) -> tuple[list[str] | None, str | None]:
    """Build a macOS-denied-by-default command for executing generated JS."""
    sandbox_executable = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or not sandbox_executable:
        return None, "网站运行时检查要求 macOS sandbox-exec，当前环境不可用"

    exact_reads = {
        smoke_script.resolve(),
        app_path.resolve(),
        manifest_path.resolve(),
    }
    system_roots = {
        path.resolve()
        for path in (
            Path("/System"),
            Path("/usr"),
            Path("/Library"),
            Path("/opt/homebrew"),
        )
        if path.exists()
    }
    metadata_paths = set()
    for path in (*exact_reads, node.resolve()):
        metadata_paths.update((path, *path.parents))
    exact_rules = " ".join(
        "(literal %s)" % _sandbox_literal(path)
        for path in sorted(exact_reads, key=lambda item: str(item))
    )
    root_rules = " ".join(
        "(subpath %s)" % _sandbox_literal(path)
        for path in sorted(system_roots, key=lambda item: str(item))
    )
    metadata_rules = " ".join(
        "(literal %s)" % _sandbox_literal(path)
        for path in sorted(metadata_paths, key=lambda item: str(item))
    )
    profile = (
        '(version 1)(deny default)(import "system.sb")'
        "(allow process-exec (literal %s))(allow process-info*)"
        "(allow file-read* %s %s)"
        "(allow file-read-metadata %s)"
        "(deny network*)(deny file-write*)"
    ) % (
        _sandbox_literal(node),
        exact_rules,
        root_rules,
        metadata_rules,
    )
    return [
        sandbox_executable,
        "-p",
        profile,
        str(node),
        str(smoke_script.resolve()),
        str(app_path.resolve()),
        str(manifest_path.resolve()),
    ], None


def check_website_runtime(
    app_path: Path,
    manifest_path: Path,
    workspace_root: Path,
    *,
    timeout_seconds: int = 10,
) -> Dict[str, Any]:
    """Launch every manifest game once in a sandboxed stub browser runtime."""
    node_value = shutil.which("node")
    if not node_value:
        return {
            "available": False,
            "passed": False,
            "message": "找不到 Node.js，无法执行网站运行时检查",
        }
    smoke_script = Path(__file__).with_name("website_runtime_smoke.js")
    if not smoke_script.is_file():
        return {
            "available": False,
            "passed": False,
            "message": "网站运行时检查器未随 Harness 安装",
        }
    command, command_error = _runtime_command(
        Path(node_value), smoke_script, app_path, manifest_path
    )
    if command_error or command is None:
        return {
            "available": False,
            "passed": False,
            "message": command_error or "无法构建网站运行时检查命令",
        }
    environment = {
        "PATH": str(Path(node_value).resolve().parent),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    try:
        result = subprocess.run(
            command,
            cwd=str(workspace_root.resolve()),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, min(timeout_seconds, 15)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "available": True,
            "passed": False,
            "timed_out": True,
            "message": "网站运行时检查超时",
        }
    except OSError as exc:
        return {
            "available": False,
            "passed": False,
            "message": "无法启动网站运行时检查：%s" % exc,
        }

    raw_output = result.stdout.strip()
    try:
        payload = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError):
        diagnostic = "\n".join(
            value.strip() for value in (result.stdout, result.stderr) if value.strip()
        )
        diagnostic = diagnostic.replace(str(workspace_root.resolve()), ".")
        if len(diagnostic) > RUNTIME_OUTPUT_LIMIT:
            diagnostic = diagnostic[:RUNTIME_OUTPUT_LIMIT] + "\n...[TRUNCATED]..."
        return {
            "available": True,
            "passed": False,
            "exit_code": result.returncode,
            "diagnostics": diagnostic or "运行时检查器未返回有效结果",
        }

    failures = payload.get("failures") if isinstance(payload, dict) else None
    failure_lines = []
    if isinstance(failures, list):
        for item in failures:
            if not isinstance(item, dict):
                continue
            game_id = str(item.get("id") or "<unknown>")
            message = str(item.get("error") or "未知错误").splitlines()[0]
            failure_lines.append("%s: %s" % (game_id, message))
    diagnostics = "\n".join(failure_lines)
    diagnostics = diagnostics.replace(str(workspace_root.resolve()), ".")
    if len(diagnostics) > RUNTIME_OUTPUT_LIMIT:
        diagnostics = diagnostics[:RUNTIME_OUTPUT_LIMIT] + "\n...[TRUNCATED]..."
    passed = bool(payload.get("passed")) and result.returncode == 0
    return {
        "available": True,
        "passed": passed,
        "exit_code": result.returncode,
        "checked_games": int(payload.get("checked_games") or 0),
        "failures": failures if isinstance(failures, list) else [],
        "diagnostics": diagnostics,
    }
