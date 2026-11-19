from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

H1_VERSION = "h1-v8-v9"
H1_BUNDLE_SHA256 = "5ff54ea07319e6e6a069a1274abd4c2f9c7a35ceb795c49f69ddcaaf8675bee8"
_ASSETS = Path(__file__).parent / "capabilities" / "h1_v8"
MAX_POINTS = 128
_XYZ_DIMENSIONS = 3
_MAX_METERS = 10
_MAX_PARAMETER = 1000


def checked_h1() -> dict[str, str]:
    """Check all published bytes before importing any trusted helper code."""
    bundle = (_ASSETS / "bundle.json").read_bytes()
    if hashlib.sha256(bundle).hexdigest() != H1_BUNDLE_SHA256:
        raise ValueError("H1 bundle hash mismatch")
    files = json.loads(bundle)
    for name in ("capability.py", "CAPABILITY.md"):
        if (_ASSETS / name).read_bytes() != files[name].encode():
            raise ValueError(f"H1 asset hash mismatch: {name}")
    return files


def h1_description() -> str:
    return checked_h1()["CAPABILITY.md"]


def resting_geometry(points: Any, **kwargs: Any) -> dict[str, Any]:
    """Only this bounded, reviewed function crosses the AST interpreter boundary."""
    checked_h1()
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
        raise ValueError(f"points must contain 1..{MAX_POINTS} XYZ rows")
    allowed = {
        "support_z",
        "known_height",
        "grip_fraction",
        "top_fraction",
        "min_points",
    }
    if kwargs.keys() - allowed:
        raise ValueError("Unknown geometry parameter")
    import math

    for row in points:
        if not isinstance(row, list) or len(row) != _XYZ_DIMENSIONS:
            raise ValueError("Each point must have exactly three coordinates")
        for value in row:
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or abs(value) > _MAX_METERS
            ):
                raise ValueError(
                    "Coordinates must be finite meter values within +/-10 m"
                )
    for value in kwargs.values():
        if value is not None and (
            type(value) not in {int, float}
            or not math.isfinite(value)
            or abs(value) > _MAX_PARAMETER
        ):
            raise ValueError("Geometry parameters must be finite bounded numbers")
    from vibe.core.hardware.capabilities.h1_v8.capability import (
        resting_geometry as trusted_geometry,
    )

    result = trusted_geometry(points, **kwargs)
    result = {
        key: value.tolist() if hasattr(value, "tolist") else value
        for key, value in result.items()
    }
    return json.loads(json.dumps(result, allow_nan=False))


__all__ = [
    "H1_BUNDLE_SHA256",
    "H1_VERSION",
    "checked_h1",
    "h1_description",
    "resting_geometry",
]
