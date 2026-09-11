"""Workspace configuration and path permission rules."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import ConfigError, WorkspaceError


CONFIG_FILENAME = ".agent-harness.json"


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _normalize_rule(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s 中包含空路径" % field_name)
    raw = value.strip().replace("\\", "/")
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigError("%s 只能包含工作区内相对路径：%s" % (field_name, value))
    normalized = candidate.as_posix().strip("/")
    if not normalized or normalized == ".":
        raise ConfigError("%s 不能指向整个工作区" % field_name)
    return normalized


def _normalize_rules(value: Any, field_name: str, *, required: bool = True) -> Tuple[str, ...]:
    if value is None and not required:
        return tuple()
    if not isinstance(value, list):
        raise ConfigError("%s 必须是字符串数组" % field_name)
    result = tuple(_normalize_rule(item, field_name) for item in value)
    if required and not result:
        raise ConfigError("%s 至少需要配置一个路径" % field_name)
    return result


def _matches_rule(relative_path: str, rules: Sequence[str]) -> bool:
    path = relative_path.strip("/")
    return any(path == rule or path.startswith(rule + "/") for rule in rules)


@dataclass(frozen=True)
class WorkspaceConfig:
    root: Path
    validation_command: Tuple[str, ...]
    validation_timeout_seconds: int
    editable_paths: Tuple[str, ...]
    protected_paths: Tuple[str, ...]
    sensitive_paths: Tuple[str, ...]
    write_all_except_protected: bool = False

    @classmethod
    def load(cls, workspace: str) -> "WorkspaceConfig":
        if not isinstance(workspace, str) or not workspace.strip():
            raise WorkspaceError(
                "invalid_workspace",
                "请输入代码项目路径",
                recoverable=True,
            )
        root = Path(workspace).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise WorkspaceError(
                "invalid_workspace",
                "代码项目不存在或不是目录：%s" % root,
                recoverable=True,
            )
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise WorkspaceError(
                "invalid_workspace",
                "不能把文件系统根目录或用户主目录本身作为代码项目",
                recoverable=True,
            )

        config_path = root / CONFIG_FILENAME
        if not config_path.is_file():
            raise ConfigError("项目根目录缺少 %s" % CONFIG_FILENAME)
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError("无法读取项目配置：%s" % exc) from exc
        if not isinstance(raw, dict):
            raise ConfigError("项目配置必须是 JSON 对象")
        if raw.get("schema_version") != 1:
            raise ConfigError("schema_version 必须为 1")

        command = raw.get("validation_command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise ConfigError("validation_command 必须是非空字符串数组")
        command = list(command)
        if command[0] in {"python", "python3"}:
            command[0] = sys.executable

        timeout = raw.get("validation_timeout_seconds", 30)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
            raise ConfigError("validation_timeout_seconds 必须是 1～300 的整数")

        editable = _normalize_rules(raw.get("editable_paths"), "editable_paths")
        protected = _normalize_rules(
            raw.get("protected_paths", []),
            "protected_paths",
            required=False,
        )
        protected = tuple(dict.fromkeys((*protected, CONFIG_FILENAME)))
        sensitive = _normalize_rules(
            raw.get("sensitive_paths", []),
            "sensitive_paths",
            required=False,
        )

        for rule in editable:
            candidate = root / rule
            if not candidate.exists() and not candidate.parent.exists():
                raise ConfigError("可编辑路径及其父目录不存在：%s" % rule)

        executable = command[0]
        if os.sep not in executable and shutil.which(executable) is None:
            raise ConfigError("找不到验证命令：%s" % executable)
        if os.sep in executable:
            executable_path = Path(executable)
            if not executable_path.is_absolute():
                executable_path = root / executable_path
            if not executable_path.exists():
                raise ConfigError("找不到验证命令：%s" % executable)

        return cls(
            root=root,
            validation_command=tuple(command),
            validation_timeout_seconds=timeout,
            editable_paths=editable,
            protected_paths=protected,
            sensitive_paths=sensitive,
            write_all_except_protected=False,
        )

    def for_autonomous_copy(self) -> "WorkspaceConfig":
        """Open visible engineering files while preserving independent boundaries."""
        common_validation_paths = (
            "tests",
            "test",
            "pytest.ini",
            "tox.ini",
            "setup.cfg",
            "pyproject.toml",
        )
        protected = tuple(
            dict.fromkeys(
                (
                    *self.protected_paths,
                    *(
                        relative
                        for relative in common_validation_paths
                        if (self.root / relative).exists()
                    ),
                )
            )
        )
        sensitive = tuple(
            dict.fromkeys(
                (
                    *self.sensitive_paths,
                    ".git",
                    ".env",
                    ".venv",
                    ".harness-tools",
                )
            )
        )
        return WorkspaceConfig(
            root=self.root,
            validation_command=self.validation_command,
            validation_timeout_seconds=self.validation_timeout_seconds,
            editable_paths=self.editable_paths,
            protected_paths=protected,
            sensitive_paths=sensitive,
            write_all_except_protected=True,
        )

    def relative(self, path: Path) -> str:
        resolved = path.resolve(strict=False)
        if not is_relative_to(resolved, self.root):
            raise WorkspaceError(
                "path_outside_workspace",
                "路径超出代码项目：%s" % path,
                recoverable=True,
            )
        return resolved.relative_to(self.root).as_posix()

    def resolve_relative(self, relative_path: str, *, allow_missing: bool = False) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise WorkspaceError(
                "invalid_tool_call",
                "文件路径不能为空",
                recoverable=True,
            )
        if "\x00" in relative_path:
            raise WorkspaceError(
                "invalid_tool_call",
                "文件路径包含非法字符",
                recoverable=True,
            )
        raw = Path(relative_path)
        if raw.is_absolute() or ".." in raw.parts:
            raise WorkspaceError(
                "path_outside_workspace",
                "只允许使用工作区相对路径：%s" % relative_path,
                recoverable=True,
            )

        candidate = self.root / raw
        current = self.root
        for part in raw.parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise WorkspaceError(
                    "unsupported_file",
                    "MVP 不支持符号链接路径：%s" % relative_path,
                    recoverable=False,
                )

        resolved = candidate.resolve(strict=False)
        if not is_relative_to(resolved, self.root):
            raise WorkspaceError(
                "path_outside_workspace",
                "路径超出代码项目：%s" % relative_path,
                recoverable=True,
            )
        if not allow_missing and not resolved.exists():
            raise WorkspaceError(
                "file_not_found",
                "文件或目录不存在：%s" % relative_path,
                recoverable=True,
            )
        return resolved

    def can_read(self, relative_path: str) -> bool:
        return not _matches_rule(relative_path, self.sensitive_paths)

    def can_write(self, relative_path: str) -> bool:
        in_scope = self.write_all_except_protected or _matches_rule(
            relative_path,
            self.editable_paths,
        )
        return (
            in_scope
            and not _matches_rule(relative_path, self.protected_paths)
            and not _matches_rule(relative_path, self.sensitive_paths)
        )

    def assert_readable(self, relative_path: str) -> None:
        if not self.can_read(relative_path):
            raise WorkspaceError(
                "path_sensitive",
                "该路径包含敏感内容，不能读取：%s" % relative_path,
                recoverable=True,
            )

    def assert_writable(self, relative_path: str) -> None:
        if _matches_rule(relative_path, self.sensitive_paths):
            code = "path_sensitive"
            message = "敏感路径不能修改"
        elif _matches_rule(relative_path, self.protected_paths):
            code = "path_protected"
            message = "只读保护路径不能修改"
        elif not (
            self.write_all_except_protected
            or _matches_rule(relative_path, self.editable_paths)
        ):
            code = "path_not_editable"
            message = "路径不在可编辑范围"
        else:
            return
        raise WorkspaceError(
            code,
            "%s：%s" % (message, relative_path),
            recoverable=True,
        )

    def public_summary(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "validation_command": list(self.validation_command),
            "validation_timeout_seconds": self.validation_timeout_seconds,
            "editable_paths": list(self.editable_paths),
            "protected_paths": list(self.protected_paths),
            "sensitive_paths": list(self.sensitive_paths),
            "write_scope": (
                "all_visible_except_protected_and_sensitive"
                if self.write_all_except_protected
                else "configured_editable_paths"
            ),
        }
