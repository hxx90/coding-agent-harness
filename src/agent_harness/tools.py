"""Harness tools exposed to the model."""

from __future__ import annotations

import json
import re
from pathlib import Path
from threading import Event
from typing import Any, Dict, List, Optional, Tuple

from .config import WorkspaceConfig
from .domain import ToolResult, ValidationResult
from .errors import AgentHarnessError, ToolExecutionError, WorkspaceError
from .helper_scripts import HelperScriptRunner
from .javascript import check_javascript_syntax
from .patching import FilePatch, apply_file_patch, parse_unified_diff
from .snapshot import SnapshotManager
from .validation import ValidationRunner
from .workspace import (
    MAX_FILE_BYTES,
    atomic_write_text,
    iter_visible_files,
    read_utf8,
    safe_file_text,
)


MAX_MUTATION_CALL_CHARS = 12_000
LEGACY_HISTORY_MARKER = "[HARNESS_COMPACTED"


def infer_registered_game_ids(source: str, manifest_ids: List[str]) -> List[str]:
    """Infer registrations without requiring the first argument to be a literal.

    Models commonly keep metadata in ``game = {id: ...}`` and call
    ``GameHub.registerGame(game.id, ...)``.  Treat that as equivalent to a
    literal registration while keeping the result limited to manifest IDs.
    """
    expected = set(manifest_ids)
    found = set()
    call_re = re.compile(
        r"(?<!function )\b(?:(?:window\.)?(?:GameHub|Hub)\.)?registerGame\s*\("
    )
    literal_re = re.compile(r"\s*(['\"])(?P<id>[^'\"]+)\1")
    object_id_re = re.compile(r"\bid\s*:\s*(['\"])(?P<id>[^'\"]+)\1")
    previous_call_end = 0

    for call in call_re.finditer(source):
        arguments = source[call.end() : call.end() + 300]
        literal = literal_re.match(arguments)
        if literal and literal.group("id") in expected:
            found.add(literal.group("id"))
        else:
            # For ``registerGame(game.id, ...)``, associate the call with the
            # nearest preceding object metadata in the same appended module.
            first_argument = arguments.split(",", 1)[0]
            if re.match(r"\s*[A-Za-z_$][\w$]*\.id\b", first_argument):
                segment = source[max(previous_call_end, call.start() - 12_000) : call.start()]
                candidates = [
                    match.group("id")
                    for match in object_id_re.finditer(segment)
                    if match.group("id") in expected
                ]
                if candidates:
                    found.add(candidates[-1])
        previous_call_end = call.end()

    return [game_id for game_id in manifest_ids if game_id in found]


def _reject_added_history_marker(text: str, *, patch: bool = False) -> None:
    lines = text.splitlines()
    if patch:
        lines = [
            line
            for line in lines
            if line.startswith("+") and not line.startswith("+++")
        ]
    if any(LEGACY_HISTORY_MARKER in line for line in lines):
        raise ToolExecutionError(
            "compacted_history_reuse",
            "历史上下文摘要不是项目代码，不能写入文件；请重新 read_file 获取真实内容",
            recoverable=True,
            details={
                "recommended_tool": "read_file",
                "recovery": "删除源码中已有的历史摘要行，再依据真实文件继续",
            },
        )


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List visible files in the workspace or a relative directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "default": ""},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 500,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search UTF-8 project text and return paths, line numbers and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "directory": {"type": "string", "default": ""},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 50,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_plan",
            "description": (
                "Record a structured plan. Supervised mode pauses for approval; "
                "autonomous mode records it and continues immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_summary": {"type": "string"},
                    "relevant_files": {"type": "array", "items": {"type": "string"}},
                    "planned_changes": {"type": "array", "items": {"type": "string"}},
                    "expected_modified_files": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "validation_plan": {"type": "string"},
                    "risks_or_questions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "task_summary",
                    "relevant_files",
                    "planned_changes",
                    "expected_modified_files",
                    "validation_plan",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Apply a standard unified diff to editable UTF-8 files. "
                "Use it for focused edits; deletion and rename are forbidden."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_MUTATION_CALL_CHARS,
                    }
                },
                "required": ["patch"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace one exact text block in an existing editable UTF-8 file. "
                "Use this for focused edits, especially long or minified lines. "
                "Include enough surrounding text to make old_string unique."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_MUTATION_CALL_CHARS,
                    },
                    "new_string": {
                        "type": "string",
                        "maxLength": MAX_MUTATION_CALL_CHARS,
                    },
                    "replace_all": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Replace every exact occurrence. Keep false unless all matches "
                            "are intentionally equivalent."
                        ),
                    },
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or replace a complete editable UTF-8 file, or append a "
                "large block to an existing file. Prefer this over a very large "
                "unified diff when generating a new project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_MUTATION_CALL_CHARS,
                        "description": (
                            "Exact content for this call. In append mode it is "
                            "concatenated exactly, so include any needed newline."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "append"],
                        "default": "replace",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_helper_script",
            "description": (
                "Create or replace an ephemeral Python helper script for this task. "
                "The script is not an engineering-code change and is stored outside the workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["name", "source"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_helper_script",
            "description": (
                "Run an ephemeral Python helper script with the workspace as its current directory. "
                "The macOS sandbox permits project reads but denies writes, network and child processes. "
                "For one-off analysis, pass source to create/replace and run the named script in one call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "source": {
                        "type": "string",
                        "maxLength": 32768,
                        "description": (
                            "Optional Python source to create/replace and execute immediately."
                        ),
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 20,
                        "default": [],
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                        "default": 10,
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_validation",
            "description": (
                "Run the workspace owner's preconfigured validation command. "
                "It accepts no model-provided command arguments."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_blocked",
            "description": "Report a concrete blocker after safe attempts cannot continue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "attempted": {"type": "array", "items": {"type": "string"}},
                    "suggested_next_step": {"type": "string"},
                },
                "required": ["reason", "attempted", "suggested_next_step"],
                "additionalProperties": False,
            },
        },
    },
]


READ_TOOLS = {"list_files", "search_code", "read_file"}
ANALYZE_ALLOWED = READ_TOOLS | {"submit_plan"}
WRITE_TOOLS = {"apply_patch", "edit_file", "write_file"}
EXECUTE_ALLOWED = READ_TOOLS | WRITE_TOOLS | {"run_validation", "report_blocked"}
HELPER_TOOLS = {"write_helper_script", "run_helper_script"}
AUTONOMOUS_ALLOWED = EXECUTE_ALLOWED | HELPER_TOOLS | {"submit_plan"}


class ToolExecutor:
    def __init__(
        self,
        config: WorkspaceConfig,
        snapshot: SnapshotManager,
        validation_runner: ValidationRunner,
        helper_dir: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.snapshot = snapshot
        self.validation_runner = validation_runner
        self.helper_runner = (
            HelperScriptRunner(
                config,
                helper_dir,
                validation_runner.redactor,
            )
            if helper_dir is not None
            else None
        )

    def execute(
        self,
        name: str,
        arguments: Dict[str, Any],
        *,
        stop_event: Optional[Event] = None,
    ) -> Tuple[ToolResult, Optional[ValidationResult]]:
        try:
            if name == "list_files":
                return self._list_files(arguments), None
            if name == "search_code":
                return self._search_code(arguments), None
            if name == "read_file":
                return self._read_file(arguments), None
            if name == "apply_patch":
                return self._apply_patch(arguments), None
            if name == "edit_file":
                return self._edit_file(arguments), None
            if name == "write_file":
                return self._write_file(arguments), None
            if name == "write_helper_script":
                return self._write_helper_script(arguments), None
            if name == "run_helper_script":
                return self._run_helper_script(arguments, stop_event), None
            if name == "run_validation":
                validation = self.validation_runner.run(stop_event)
                return ToolResult.success(**validation.to_dict()), validation
            return (
                ToolResult.failure(
                    "unknown_tool",
                    "未知工具：%s" % name,
                    recoverable=True,
                ),
                None,
            )
        except AgentHarnessError as exc:
            return (
                ToolResult.failure(
                    exc.code,
                    exc.message,
                    recoverable=exc.recoverable,
                    **exc.details,
                ),
                None,
            )
        except (TypeError, ValueError) as exc:
            return (
                ToolResult.failure(
                    "invalid_tool_call",
                    "工具参数不合法：%s" % exc,
                    recoverable=True,
                ),
                None,
            )
        except Exception as exc:
            return (
                ToolResult.failure(
                    "tool_internal_error",
                    "工具执行异常：%s" % exc,
                    recoverable=False,
                ),
                None,
            )

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("数量参数必须是整数")
        return max(minimum, min(maximum, value))

    def _list_files(self, arguments: Dict[str, Any]) -> ToolResult:
        directory = arguments.get("directory", "")
        max_results = self._bounded_int(arguments.get("max_results"), 500, 1, 500)
        if not isinstance(directory, str):
            raise ValueError("directory 必须是字符串")
        start = self.config.root
        if directory.strip():
            start = self.config.resolve_relative(directory)
            relative = start.relative_to(self.config.root).as_posix()
            self.config.assert_readable(relative)
            if not start.is_dir():
                raise WorkspaceError(
                    "file_not_found",
                    "目录不存在：%s" % directory,
                    recoverable=True,
                )
        files: List[str] = []
        truncated = False
        for relative, _ in iter_visible_files(self.config, start):
            if len(files) >= max_results:
                truncated = True
                break
            files.append(relative)
        return ToolResult.success(files=files, truncated=truncated)

    def _search_code(self, arguments: Dict[str, Any]) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query or len(query) > 200:
            raise ValueError("query 必须是 1～200 字符的字符串")
        directory = arguments.get("directory", "")
        if not isinstance(directory, str):
            raise ValueError("directory 必须是字符串")
        max_results = self._bounded_int(arguments.get("max_results"), 50, 1, 50)
        start = self.config.root
        if directory.strip():
            start = self.config.resolve_relative(directory)
            relative = start.relative_to(self.config.root).as_posix()
            self.config.assert_readable(relative)
        matches: List[Dict[str, Any]] = []
        for relative, path in iter_visible_files(self.config, start):
            try:
                lines = read_utf8(path).splitlines()
            except AgentHarnessError:
                continue
            for index, line in enumerate(lines):
                if query not in line:
                    continue
                lower = max(0, index - 2)
                upper = min(len(lines), index + 3)
                matches.append(
                    {
                        "path": relative,
                        "line": index + 1,
                        "snippet": "\n".join(
                            "%d: %s" % (line_number + 1, lines[line_number])
                            for line_number in range(lower, upper)
                        ),
                    }
                )
                if len(matches) >= max_results:
                    return ToolResult.success(matches=matches, truncated=True)
        return ToolResult.success(matches=matches, truncated=False)

    def _read_file(self, arguments: Dict[str, Any]) -> ToolResult:
        relative = arguments.get("path")
        if not isinstance(relative, str):
            raise ValueError("path 必须是字符串")
        start_line = self._bounded_int(arguments.get("start_line"), 1, 1, 1_000_000)
        end_value = arguments.get("end_line")
        end_line = (
            self._bounded_int(end_value, start_line + 1999, start_line, 1_000_000)
            if end_value is not None
            else start_line + 1999
        )
        content = safe_file_text(self.config, relative)
        lines = content.splitlines()
        selected = lines[start_line - 1 : end_line]
        numbered = "\n".join(
            "%d: %s" % (start_line + offset, line)
            for offset, line in enumerate(selected)
        )
        truncated = end_line < len(lines)
        if len(numbered) > 200 * 1024:
            numbered = numbered[: 200 * 1024] + "\n...[TRUNCATED]..."
            truncated = True
        return ToolResult.success(
            path=relative,
            content=numbered,
            start_line=start_line,
            end_line=min(end_line, len(lines)),
            total_lines=len(lines),
            truncated=truncated,
        )

    def _apply_patch(self, arguments: Dict[str, Any]) -> ToolResult:
        patch_text = arguments.get("patch")
        if not isinstance(patch_text, str):
            raise ValueError("patch 必须是字符串")
        if not patch_text:
            raise ValueError("patch 不能为空")
        # Deleting an already leaked marker is allowed; adding one is not.
        _reject_added_history_marker(patch_text, patch=True)
        if len(patch_text) > MAX_MUTATION_CALL_CHARS:
            raise ToolExecutionError(
                "patch_chunk_too_large",
                "单次 Patch 不能超过 %s 字符，请拆成多个局部修改"
                % MAX_MUTATION_CALL_CHARS,
                recoverable=True,
                details={"max_chars": MAX_MUTATION_CALL_CHARS},
            )
        file_patches = parse_unified_diff(patch_text)
        prepared: List[Tuple[FilePatch, Path, str, Optional[int], bool]] = []
        normalized_hunk_counts: List[Dict[str, Any]] = []

        for file_patch in file_patches:
            path = self.config.resolve_relative(file_patch.path, allow_missing=True)
            relative = path.relative_to(self.config.root).as_posix()
            self.config.assert_writable(relative)
            self.snapshot.assert_current(relative)
            exists = path.exists()
            if file_patch.is_new and exists:
                raise ToolExecutionError(
                    "patch_conflict",
                    "新文件已经存在：%s" % relative,
                    recoverable=True,
                )
            if not file_patch.is_new and not exists:
                raise ToolExecutionError(
                    "patch_conflict",
                    "要更新的文件不存在：%s" % relative,
                    recoverable=True,
                )
            original = read_utf8(path) if exists else ""
            mode = (path.stat().st_mode & 0o777) if exists else 0o644
            try:
                updated = apply_file_patch(original, file_patch)
            except ToolExecutionError as exc:
                if exc.code != "patch_conflict":
                    raise
                raise ToolExecutionError(
                    exc.code,
                    exc.message
                    + "。请重新读取最新片段；若目的是在文件末尾增加代码，"
                    "请改用 write_file 的 append 模式，不要重复猜测行号",
                    recoverable=True,
                    details={
                        **exc.details,
                        "recommended_tool": "write_file",
                        "recommended_mode": "append",
                    },
                ) from exc
            if updated == original:
                raise ToolExecutionError(
                    "patch_no_changes",
                    "Patch 没有产生任何内容变化：%s" % relative,
                    recoverable=True,
                )
            for index, hunk in enumerate(file_patch.hunks, start=1):
                actual_old = sum(
                    1 for line in hunk.lines if line[0] in {" ", "-", "?"}
                )
                actual_new = sum(
                    1 for line in hunk.lines if line[0] in {" ", "+", "?"}
                )
                if actual_old != hunk.old_count or actual_new != hunk.new_count:
                    normalized_hunk_counts.append(
                        {
                            "path": relative,
                            "hunk": index,
                            "declared_old_count": hunk.old_count,
                            "actual_old_count": actual_old,
                            "declared_new_count": hunk.new_count,
                            "actual_new_count": actual_new,
                        }
                    )
            if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
                raise ToolExecutionError(
                    "unsupported_file",
                    "修改后文件超过 1 MB：%s" % relative,
                    recoverable=False,
                )
            prepared.append((file_patch, path, updated, mode, exists))

        backups: List[Tuple[Path, str, Optional[int], bool]] = []
        changed: List[str] = []
        try:
            for file_patch, path, updated, mode, existed in prepared:
                original = read_utf8(path) if existed else ""
                backups.append((path, original, mode, existed))
                atomic_write_text(path, updated, mode)
                changed.append(path.relative_to(self.config.root).as_posix())
        except Exception:
            for path, original, mode, existed in reversed(backups):
                try:
                    if existed:
                        atomic_write_text(path, original, mode)
                    elif path.exists():
                        path.unlink()
                except Exception:
                    pass
            raise

        self.snapshot.mark_changed(changed)
        added_lines = sum(
            1
            for line in patch_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        removed_lines = sum(
            1
            for line in patch_text.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        extra: Dict[str, Any] = {}
        syntax_checks = []
        for relative in changed:
            path = self.config.root / relative
            if path.suffix.lower() == ".js" and path.is_file():
                syntax = check_javascript_syntax(path, self.config.root)
                syntax_checks.append({"path": relative, **syntax})
                if relative == "app.js":
                    extra.update(self._website_progress(relative, read_utf8(path)))
        if syntax_checks:
            extra["syntax_checks"] = syntax_checks
            if any(not item.get("passed") for item in syntax_checks):
                progress = extra.get("website_progress")
                if isinstance(progress, dict):
                    progress["next_action"] = (
                        "先根据 syntax_checks 修复 JavaScript 语法错误，再继续功能或验证"
                    )
        return ToolResult.success(
            changed_files=changed,
            normalized_hunk_counts=normalized_hunk_counts,
            diff_summary={
                "added_lines": added_lines,
                "removed_lines": removed_lines,
            },
            **extra,
        )

    def _write_file(self, arguments: Dict[str, Any]) -> ToolResult:
        relative_argument = arguments.get("path")
        content = arguments.get("content")
        mode_name = arguments.get("mode", "replace")
        if not isinstance(relative_argument, str):
            raise ValueError("path 必须是字符串")
        if not isinstance(content, str):
            raise ValueError("content 必须是字符串")
        if not content:
            raise ValueError("content 不能为空")
        _reject_added_history_marker(content)
        if len(content) > MAX_MUTATION_CALL_CHARS:
            raise ToolExecutionError(
                "write_chunk_too_large",
                "单次写入不能超过 %s 字符，请先 replace 核心结构，再分批 append"
                % MAX_MUTATION_CALL_CHARS,
                recoverable=True,
                details={"max_chars": MAX_MUTATION_CALL_CHARS},
            )
        if mode_name not in {"replace", "append"}:
            raise ValueError("mode 只能是 replace 或 append")

        path = self.config.resolve_relative(relative_argument, allow_missing=True)
        relative = path.relative_to(self.config.root).as_posix()
        self.config.assert_writable(relative)
        self.snapshot.assert_current(relative)
        exists = path.exists()
        if exists and not path.is_file():
            raise ToolExecutionError(
                "unsupported_file",
                "只能写入普通 UTF-8 文件：%s" % relative,
                recoverable=True,
            )
        if mode_name == "append" and not exists:
            raise ToolExecutionError(
                "file_not_found",
                "append 模式要求文件已存在：%s" % relative,
                recoverable=True,
            )

        original = read_utf8(path) if exists else ""
        inserted_separator = False
        if mode_name == "append":
            separator = ""
            if (
                original
                and not original.endswith(("\n", "\r"))
                and not content.startswith(("\n", "\r"))
            ):
                separator = "\n"
                inserted_separator = True
            updated = original + separator + content
        else:
            updated = content
        if updated == original:
            raise ToolExecutionError(
                "write_no_changes",
                "写入内容没有产生变化：%s" % relative,
                recoverable=True,
            )
        byte_count = len(updated.encode("utf-8"))
        if byte_count > MAX_FILE_BYTES:
            raise ToolExecutionError(
                "unsupported_file",
                "写入后文件超过 1 MB：%s" % relative,
                recoverable=False,
            )

        file_mode = (path.stat().st_mode & 0o777) if exists else 0o644
        try:
            atomic_write_text(path, updated, file_mode)
            self.snapshot.mark_changed([relative])
        except Exception:
            try:
                if exists:
                    atomic_write_text(path, original, file_mode)
                elif path.exists():
                    path.unlink()
            except Exception:
                pass
            raise

        website_progress = self._website_progress(relative, updated)
        syntax_check: Dict[str, Any] = {}
        if path.suffix.lower() == ".js":
            syntax_check = check_javascript_syntax(path, self.config.root)
            if not syntax_check.get("passed"):
                progress = website_progress.get("website_progress")
                if isinstance(progress, dict):
                    progress["next_action"] = (
                        "先根据 syntax_check 修复 JavaScript 语法错误，再继续追加或验证"
                    )
        return ToolResult.success(
            changed_files=[relative],
            operation=("appended" if mode_name == "append" else "written"),
            bytes=byte_count,
            inserted_separator=inserted_separator,
            diff_summary={
                "added_lines": len(content.splitlines()) + int(inserted_separator),
                "removed_lines": (
                    len(original.splitlines()) if mode_name == "replace" else 0
                ),
            },
            **({"syntax_check": syntax_check} if syntax_check else {}),
            **website_progress,
        )

    def _edit_file(self, arguments: Dict[str, Any]) -> ToolResult:
        relative_argument = arguments.get("path")
        old_string = arguments.get("old_string")
        new_string = arguments.get("new_string")
        replace_all = arguments.get("replace_all", False)
        if not isinstance(relative_argument, str):
            raise ValueError("path 必须是字符串")
        if not isinstance(old_string, str) or not old_string:
            raise ValueError("old_string 必须是非空字符串")
        if not isinstance(new_string, str):
            raise ValueError("new_string 必须是字符串")
        if not isinstance(replace_all, bool):
            raise ValueError("replace_all 必须是布尔值")
        if old_string == new_string:
            raise ToolExecutionError(
                "edit_no_changes",
                "old_string 与 new_string 相同",
                recoverable=True,
            )
        if (
            len(old_string) > MAX_MUTATION_CALL_CHARS
            or len(new_string) > MAX_MUTATION_CALL_CHARS
        ):
            raise ToolExecutionError(
                "edit_chunk_too_large",
                "old_string 和 new_string 分别不能超过 %s 字符"
                % MAX_MUTATION_CALL_CHARS,
                recoverable=True,
                details={"max_chars_each": MAX_MUTATION_CALL_CHARS},
            )
        _reject_added_history_marker(new_string)

        path = self.config.resolve_relative(relative_argument)
        relative = path.relative_to(self.config.root).as_posix()
        self.config.assert_writable(relative)
        self.snapshot.assert_current(relative)
        if not path.is_file():
            raise ToolExecutionError(
                "file_not_found",
                "edit_file 要求文件已存在：%s" % relative,
                recoverable=True,
            )

        original = read_utf8(path)
        line_ending = "\r\n" if "\r\n" in original else "\n"
        normalized = original.replace("\r\n", "\n")
        old_normalized = old_string.replace("\r\n", "\n")
        new_normalized = new_string.replace("\r\n", "\n")
        occurrences = normalized.count(old_normalized)
        if occurrences == 0:
            raise ToolExecutionError(
                "edit_not_found",
                "未找到完全匹配的 old_string：%s" % relative,
                recoverable=True,
                details={
                    "recommended_tools": ["search_code", "read_file"],
                    "recovery": "重新读取最新内容，并给 old_string 增加足够但准确的上下文",
                },
            )
        if occurrences > 1 and not replace_all:
            raise ToolExecutionError(
                "edit_ambiguous",
                "old_string 在 %s 中出现 %s 次，无法确定唯一修改位置"
                % (relative, occurrences),
                recoverable=True,
                details={
                    "occurrences": occurrences,
                    "recovery": (
                        "扩大 old_string 上下文使其唯一；只有全部匹配都应修改时才使用 replace_all"
                    ),
                },
            )

        replacements = occurrences if replace_all else 1
        updated_normalized = normalized.replace(
            old_normalized,
            new_normalized,
            -1 if replace_all else 1,
        )
        updated = (
            updated_normalized.replace("\n", "\r\n")
            if line_ending == "\r\n"
            else updated_normalized
        )
        if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
            raise ToolExecutionError(
                "unsupported_file",
                "修改后文件超过 1 MB：%s" % relative,
                recoverable=False,
            )

        file_mode = path.stat().st_mode & 0o777
        try:
            atomic_write_text(path, updated, file_mode)
            self.snapshot.mark_changed([relative])
        except Exception:
            try:
                atomic_write_text(path, original, file_mode)
            except Exception:
                pass
            raise

        website_progress = self._website_progress(relative, updated)
        syntax_check: Dict[str, Any] = {}
        if path.suffix.lower() == ".js":
            syntax_check = check_javascript_syntax(path, self.config.root)
            if not syntax_check.get("passed"):
                progress = website_progress.get("website_progress")
                if isinstance(progress, dict):
                    progress["next_action"] = (
                        "先根据 syntax_check 继续用 edit_file 修复 JavaScript 语法错误"
                    )
        return ToolResult.success(
            changed_files=[relative],
            operation="edited",
            matched_occurrences=occurrences,
            replacements=replacements,
            diff_summary={
                "added_lines": len(new_string.splitlines()),
                "removed_lines": len(old_string.splitlines()),
            },
            **({"syntax_check": syntax_check} if syntax_check else {}),
            **website_progress,
        )

    def _website_progress(self, relative: str, source: str) -> Dict[str, Any]:
        if relative != "app.js":
            return {}
        manifest_path = self.config.root / "game-manifest.json"
        requirements_path = self.config.root / "site-requirements.json"
        if not manifest_path.is_file() or not requirements_path.is_file():
            return {}
        try:
            manifest = json.loads(read_utf8(manifest_path))
            requirements = json.loads(read_utf8(requirements_path))
        except (AgentHarnessError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(manifest, list) or not isinstance(requirements, dict):
            return {}
        expected = requirements.get("expected_game_count")
        ids = [
            str(item.get("id"))
            for item in manifest
            if isinstance(item, dict) and item.get("id")
        ]
        if not isinstance(expected, int) or expected <= 0 or not ids:
            return {}
        registered = infer_registered_game_ids(source, ids)
        missing = [game_id for game_id in ids if game_id not in registered]
        leaked_markers = source.count(LEGACY_HISTORY_MARKER)
        progress: Dict[str, Any] = {
            "expected_count": expected,
            "registered_count": len(registered),
            "registered_ids": registered,
            "missing_ids": missing,
            "next_action": (
                "先删除 app.js 中已有的历史上下文摘要行，再继续实现 missing_ids"
                if leaked_markers
                else (
                    "继续使用 write_file append，每批最多补 2 个 missing_ids；"
                    "不要重写已经成功的游戏"
                    if missing
                    else "全部清单 ID 已注册，请运行 run_validation"
                )
            ),
        }
        if leaked_markers:
            progress["source_issues"] = [
                "检测到 %s 行历史上下文摘要被误写入 app.js；这些不是代码，必须删除"
                % leaked_markers
            ]
        if re.search(r"window\.(?:GameHub|Hub)\s*=", source):
            progress["architecture_hint"] = (
                "已检测到 window.Hub/GameHub 公共 API；核心闭包结束后追加独立游戏 IIFE "
                "是合法结构，不要把模块移回核心闭包"
            )
        return {"website_progress": progress}

    def _write_helper_script(self, arguments: Dict[str, Any]) -> ToolResult:
        if self.helper_runner is None:
            return ToolResult.failure(
                "helper_scripts_unavailable",
                "当前 Task 未启用临时辅助脚本",
                recoverable=True,
            )
        return ToolResult.success(
            **self.helper_runner.write(
                arguments.get("name"),
                arguments.get("source"),
            )
        )

    def _run_helper_script(
        self,
        arguments: Dict[str, Any],
        stop_event: Optional[Event],
    ) -> ToolResult:
        if self.helper_runner is None:
            return ToolResult.failure(
                "helper_scripts_unavailable",
                "当前 Task 未启用临时辅助脚本",
                recoverable=True,
            )
        script_write = None
        if "source" in arguments:
            script_write = self.helper_runner.write(
                arguments.get("name"),
                arguments.get("source"),
            )
        result = self.helper_runner.run(
            arguments.get("name"),
            arguments.get("args", []),
            arguments.get("timeout_seconds", 10),
            stop_event,
        )
        if script_write is not None:
            result["script_write"] = script_write
        return ToolResult.success(**result)
