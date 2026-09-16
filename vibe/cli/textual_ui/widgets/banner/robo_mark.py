from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.widgets import Static

from vibe.cli.textual_ui.widgets.braille_renderer import render_braille

WIDTH = 22
HEIGHT = 12

# A compact robot-radish drawn on the same 22x12 braille canvas as the former
# mascot. Keeping the canvas size stable avoids layout shifts in every surface.
_ROBO_ROWS = (
    ".......##....##.......",
    "........##..##........",
    ".....##..####..##.....",
    "......##########......",
    "....##############....",
    "...#####..##..#####...",
    "...#####..##..#####...",
    "...###############....",
    "....#############.....",
    "......#########.......",
    "........#####.........",
    "..........#...........",
)
_X_OFFSET = 0
_Y_OFFSET = 0
ROBO_DOTS = {
    complex(x + _X_OFFSET, y + _Y_OFFSET)
    for y, row in enumerate(_ROBO_ROWS)
    for x, pixel in enumerate(row)
    if pixel == "#"
}


class RoboMark(Static):
    """Static Robo radish mark used by the banner and setup screens."""

    def __init__(self, animate: bool = True, **kwargs: Any) -> None:
        del animate  # Kept for compatibility with the former animated mark.
        classes = kwargs.pop("classes", None)
        merged_classes = "banner-chat" if classes is None else f"banner-chat {classes}"
        super().__init__(**kwargs, classes=merged_classes)

    def compose(self) -> ComposeResult:
        yield Static(render_braille(ROBO_DOTS, WIDTH, HEIGHT), classes="petit-chat")

    def freeze_animation(self) -> None:
        """Compatibility no-op: the Robo wordmark is intentionally static."""


__all__ = ["RoboMark"]
