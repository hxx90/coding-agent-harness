"""A small, strict unified-diff parser and applier."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from .errors import ToolExecutionError


HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
HUNK_RELOCATION_WINDOW = 100


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: List[str] = field(default_factory=list)


@dataclass
class FilePatch:
    path: str
    is_new: bool
    hunks: List[Hunk] = field(default_factory=list)


def _hunk_matches_at(old_lines: List[str], start: int, hunk: Hunk) -> bool:
    cursor = start
    has_old_side = False
    for raw_line in hunk.lines:
        prefix = raw_line[0]
        text = raw_line[1:]
        if prefix in {" ", "-"}:
            has_old_side = True
            if cursor >= len(old_lines) or old_lines[cursor] != text:
                return False
            cursor += 1
        elif prefix == "?" and cursor < len(old_lines) and old_lines[cursor] == "":
            cursor += 1
    return has_old_side


def _resolve_hunk_target(
    old_lines: List[str], declared: int, cursor: int, hunk: Hunk
) -> int:
    if cursor <= declared <= len(old_lines) and _hunk_matches_at(
        old_lines, declared, hunk
    ):
        return declared
    lower = max(cursor, declared - HUNK_RELOCATION_WINDOW)
    upper = min(len(old_lines), declared + HUNK_RELOCATION_WINDOW)
    candidates = [
        candidate
        for candidate in range(lower, upper + 1)
        if _hunk_matches_at(old_lines, candidate, hunk)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return declared


def _header_path(line: str, prefix: str) -> str:
    value = line[len(prefix) :].split("\t", 1)[0].strip()
    if value in {"/dev/null", "dev/null"}:
        return "/dev/null"
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    return value


def parse_unified_diff(patch_text: str) -> List[FilePatch]:
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise ToolExecutionError(
            "patch_invalid",
            "Patch 不能为空",
            recoverable=True,
        )
    lines = patch_text.splitlines()
    patches: List[FilePatch] = []
    index = 0

    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        old_path = _header_path(lines[index], "--- ")
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ToolExecutionError(
                "patch_invalid",
                "每个 --- 文件头后必须紧跟 +++ 文件头",
                recoverable=True,
            )
        new_path = _header_path(lines[index], "+++ ")
        index += 1
        if new_path == "/dev/null":
            raise ToolExecutionError(
                "patch_delete_forbidden",
                "MVP 不允许删除已有文件",
                recoverable=True,
            )
        is_new = old_path == "/dev/null"
        if not is_new and old_path != new_path:
            raise ToolExecutionError(
                "patch_rename_forbidden",
                "MVP 不支持重命名文件：%s → %s" % (old_path, new_path),
                recoverable=True,
            )

        file_patch = FilePatch(path=new_path, is_new=is_new)
        while index < len(lines):
            if lines[index].startswith("--- "):
                break
            match = HUNK_RE.match(lines[index])
            if not match:
                index += 1
                continue
            hunk = Hunk(
                old_start=int(match.group("old_start")),
                old_count=int(match.group("old_count") or "1"),
                new_start=int(match.group("new_start")),
                new_count=int(match.group("new_count") or "1"),
            )
            index += 1
            while index < len(lines):
                line = lines[index]
                if HUNK_RE.match(line) or line.startswith("--- "):
                    break
                if line.startswith("\\ No newline at end of file"):
                    index += 1
                    continue
                if not line:
                    # Some tool-calling models omit the required prefix on a
                    # blank diff line.  Keep it as an internal ambiguous marker;
                    # the applier can decide whether it is empty context or an
                    # added blank line from the current source cursor.
                    next_index = index + 1
                    while next_index < len(lines) and not lines[next_index]:
                        next_index += 1
                    if next_index < len(lines) and (
                        HUNK_RE.match(lines[next_index])
                        or lines[next_index].startswith("--- ")
                    ):
                        index = next_index
                        break
                    hunk.lines.append("?")
                    index += 1
                    continue
                if line[0] not in {" ", "+", "-"}:
                    raise ToolExecutionError(
                        "patch_invalid",
                        "Hunk 中存在非法行：%s" % line[:120],
                        recoverable=True,
                    )
                hunk.lines.append(line)
                index += 1
            file_patch.hunks.append(hunk)

        if not file_patch.hunks:
            raise ToolExecutionError(
                "patch_invalid",
                "文件 Patch 缺少 @@ hunk：%s" % new_path,
                recoverable=True,
            )
        patches.append(file_patch)

    if not patches:
        raise ToolExecutionError(
            "patch_invalid",
            "未找到标准 unified diff 文件头",
            recoverable=True,
        )
    paths = [item.path for item in patches]
    if len(set(paths)) != len(paths):
        raise ToolExecutionError(
            "patch_invalid",
            "同一文件不能在一个 Patch 中出现多次",
            recoverable=True,
        )
    return patches


def apply_file_patch(original: str, patch: FilePatch) -> str:
    if patch.is_new and original:
        raise ToolExecutionError(
            "patch_conflict",
            "新文件 Patch 的目标已经存在：%s" % patch.path,
            recoverable=True,
        )

    old_lines = original.splitlines()
    output: List[str] = []
    cursor = 0

    for hunk in patch.hunks:
        declared_target = max(hunk.old_start - 1, 0)
        target = _resolve_hunk_target(
            old_lines,
            declared_target,
            cursor,
            hunk,
        )
        if target < cursor or target > len(old_lines):
            raise ToolExecutionError(
                "patch_conflict",
                "Hunk 行号与当前文件不匹配：%s" % patch.path,
                recoverable=True,
            )
        output.extend(old_lines[cursor:target])
        cursor = target
        consumed_old = 0
        produced_new = 0

        for raw_line in hunk.lines:
            prefix = raw_line[0]
            text = raw_line[1:]
            if prefix in {" ", "-"}:
                if cursor >= len(old_lines) or old_lines[cursor] != text:
                    actual = old_lines[cursor] if cursor < len(old_lines) else "<EOF>"
                    raise ToolExecutionError(
                        "patch_conflict",
                        "Patch 上下文不匹配：%s，期望 %r，实际 %r"
                        % (patch.path, text, actual),
                        recoverable=True,
                    )
                if prefix == " ":
                    output.append(text)
                    produced_new += 1
                cursor += 1
                consumed_old += 1
            elif prefix == "+":
                output.append(text)
                produced_new += 1
            elif prefix == "?":
                if cursor < len(old_lines) and old_lines[cursor] == "":
                    output.append("")
                    cursor += 1
                    consumed_old += 1
                    produced_new += 1
                else:
                    output.append("")
                    produced_new += 1

        # LLMs frequently miscount redundant hunk header totals. The exact
        # context above is still matched line by line, so deriving the counts
        # from the hunk body is safe and avoids format-only retry loops.

    output.extend(old_lines[cursor:])
    if not output:
        return ""
    return "\n".join(output) + "\n"
