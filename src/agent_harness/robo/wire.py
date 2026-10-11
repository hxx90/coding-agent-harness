"""JSON values plus bounded, lossless numerical arrays for device adapters.

No pickle, executable payloads or ambient imports. NumPy is only required for
array values and must be explicitly available in the frozen worker runtime.
"""

from __future__ import annotations

import base64
import math
import zlib
from typing import Any

MAX_ARRAY_BYTES = 16 * 1024 * 1024
MAX_WIRE = 24 * 1024 * 1024


def encode(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if type(value).__module__.split(".")[0] == "numpy":
        import numpy as np

        if isinstance(value, np.generic):
            return encode(value.item())
        array = np.ascontiguousarray(value)
        if array.dtype.kind not in "biuf" or array.nbytes > MAX_ARRAY_BYTES:
            raise ValueError("Unsupported or oversized numerical array")
        return {
            "__robo_array__": 1,
            "dtype": array.dtype.str,
            "shape": list(array.shape),
            "data": base64.b64encode(zlib.compress(array.tobytes(), 1)).decode(),
        }
    return value


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {"__robo_array__", "dtype", "shape", "data"}:
            import numpy as np

            shape = value["shape"]
            dtype = np.dtype(value["dtype"])
            if (
                value["__robo_array__"] != 1
                or dtype.kind not in "biuf"
                or not isinstance(shape, list)
                or len(shape) > 8
                or any(type(n) is not int or n < 0 for n in shape)
            ):
                raise ValueError("Invalid numerical array")
            expected = math.prod(shape) * dtype.itemsize
            if expected > MAX_ARRAY_BYTES or len(value["data"]) > MAX_WIRE:
                raise ValueError("Numerical array exceeds wire budget")
            inflater = zlib.decompressobj()
            data = inflater.decompress(
                base64.b64decode(value["data"], validate=True), expected + 1
            )
            if len(data) != expected or not inflater.eof or inflater.unused_data:
                raise ValueError("Invalid numerical array size")
            return np.frombuffer(data, dtype=dtype).reshape(shape).copy()
        return {k: decode(v) for k, v in value.items()}
    return value
