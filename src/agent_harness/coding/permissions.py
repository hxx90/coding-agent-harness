"""A single permission decision point for every agent-triggered operation."""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Sequence

from .types import CodingError, Json

Approval = Callable[[str, str], str]
READ_TOOLS = {"read", "glob", "grep", "skill", "todo_read"}
PLAN_TOOLS = READ_TOOLS | {"todo_write", "question", "task"}


class Permissions:
    """Project access is additionally checked by Workspace; Shell is trusted execution."""

    def __init__(
        self,
        rules: Sequence[Json] = (),
        *,
        mode: str = "build",
        approve: Approval | None = None,
    ) -> None:
        self.rules = list(rules)
        self.mode = mode
        self.approve = approve
        self.grants: set[tuple[str, str]] = set()

    def action(self, tool: str, target: str) -> str:
        if self.mode == "plan" and tool not in PLAN_TOOLS:
            return "deny"
        action = (
            "allow"
            if tool in READ_TOOLS | {"todo_write", "question", "task"}
            else "ask"
        )
        for rule in self.rules:
            if fnmatch.fnmatchcase(tool, rule["tool"]) and fnmatch.fnmatchcase(
                target, rule.get("pattern", "*")
            ):
                action = str(rule["action"])
        if action == "ask" and (tool, target) in self.grants:
            return "allow"
        return action

    def require(self, tool: str, target: str, *, preview: str | None = None) -> None:
        action = self.action(tool, target)
        grant = (tool, preview or target)
        if action == "ask" and grant in self.grants:
            action = "allow"
        if action == "allow":
            return
        if action == "deny":
            raise CodingError("permission_denied", f"{tool} is denied for {target}")
        if self.approve is None:
            raise CodingError(
                "permission_required",
                f"{tool} requires approval: {target}. Run interactively or grant --allow {tool}.",
                details={"tool": tool, "target": target},
            )
        decision = self.approve(tool, preview or target)
        if decision == "session":
            self.grants.add(grant)
        elif decision != "once":
            raise CodingError("permission_denied", f"User denied {tool}: {target}")
