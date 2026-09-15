"""Exact macOS Python launcher images and metadata needed to resolve them."""

from __future__ import annotations

import hashlib
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
    # venv --copies keeps a launcher outside the framework. Trust pyvenv's base
    # image only when its executable bytes match the actual copied launcher.
    config = executable.parent.parent / "pyvenv.cfg"
    if config.is_file() and not any(
        p.name.endswith(".framework") for p in resolved.parents
    ):
        values = dict(
            line.split(" = ", 1)
            for line in config.read_text().splitlines()
            if " = " in line
        )
        base = Path(values.get("executable", ""))
        if (
            base.is_file()
            and hashlib.sha256(base.read_bytes()).digest()
            == hashlib.sha256(resolved.read_bytes()).digest()
        ):
            resolved = base.resolve()
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
