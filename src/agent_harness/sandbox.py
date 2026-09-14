"""Exact macOS Python launcher images and metadata needed to resolve them."""

from __future__ import annotations

import json
from pathlib import Path


def python_launcher_rules(executable: Path) -> str:
    """Homebrew framework launchers spawn a second, fixed interpreter image.

    Resolving the venv -> opt -> Cellar symlink chain also requires metadata
    within the Python installation, beyond the framework's stdlib directory.
    These rules grant no additional file contents, writes or network access.
    """
    resolved = executable.resolve()
    if not resolved.name.startswith("python"):
        return ""
    framework = next(
        (path for path in resolved.parents if path.name.endswith(".framework")), None
    )
    if framework is None:
        return ""
    image = (
        resolved.parent.parent
        / "Resources"
        / "Python.app"
        / "Contents"
        / "MacOS"
        / "Python"
    )
    if not image.is_file():
        return ""
    installation = framework.parent.parent
    roots = {installation, executable.parent, resolved.parent}
    current = executable
    for _ in range(20):
        if not current.is_symlink():
            break
        target = current.readlink()
        current = target if target.is_absolute() else current.parent / target
        roots.add(current.parent)
    parents = {parent for path in roots for parent in path.parents}
    metadata = " ".join(
        f"(subpath {json.dumps(str(path))})" for path in sorted(roots, key=str)
    )
    metadata += " " + " ".join(
        f"(literal {json.dumps(str(path))})" for path in sorted(parents, key=str)
    )
    return f"(allow process-exec (literal {json.dumps(str(image))}))(allow file-read-metadata {metadata})"
