"""Attribute reversible changes to tool execution, never model waiting time."""

from __future__ import annotations

from collections.abc import Callable

from .types import Json
from .workspace import Workspace

FILE_TOOLS = {"write", "edit", "apply_patch"}


class ChangeJournal:
    def __init__(
        self, workspace: Workspace, state: Json, save: Callable[[], None]
    ) -> None:
        self.workspace = workspace
        self.state = state
        self.save = save

    def execute(self, tool: str, operation: Callable[[], Json]) -> Json:
        self.state["active"]["effect"] = {
            "tool": tool,
            "before": {} if tool in FILE_TOOLS else self.workspace.capture(),
        }
        self.save()
        try:
            return operation()
        finally:
            self.finish()

    def before_write(self, name: str, before: Json | None) -> None:
        effect = self.state["active"].get("effect")
        if effect is not None and name not in effect["before"]:
            effect["before"][name] = before
            self.workspace.extra_paths.add(name)
            self.save()

    def finish(self, *, uncertain: bool = False) -> None:
        active = self.state["active"]
        effect = active.get("effect")
        if not effect:
            return
        before = effect["before"]
        after = (
            {name: self.workspace.snapshot_file(name) for name in before}
            if effect["tool"] in FILE_TOOLS
            else self.workspace.capture()
        )
        changes = active.setdefault("changes", {})
        for name in before.keys() | after.keys():
            previous, current = before.get(name), after.get(name)
            if previous == current:
                continue
            recorded = changes.get(name)
            # A user edit between tools supersedes older undo authority. Preserve
            # the user's latest version as the new restoration target.
            original = (
                recorded["before"]
                if recorded and recorded["after"] == previous
                else previous
            )
            if original == current:
                changes.pop(name, None)
            else:
                changes[name] = {"before": original, "after": current}
        if uncertain:
            active["uncertain"] = True
        active.pop("effect", None)
        self.save()
