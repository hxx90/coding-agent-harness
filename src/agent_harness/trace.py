"""Append-only local Trace with basic secret redaction."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .errors import TraceWriteError


SENSITIVE_NAME_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|API[_-]?KEY)", re.IGNORECASE)
INLINE_SECRET_RE = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}|"
    r"((?:token|secret|password|api[_-]?key)\s*[:=]\s*)[^\s,;]+"
)


class Redactor:
    def __init__(self, secret_values: Optional[Iterable[str]] = None) -> None:
        values = set(secret_values or [])
        for name, value in os.environ.items():
            if SENSITIVE_NAME_RE.search(name) and len(value) >= 8:
                values.add(value)
        self.secret_values = sorted(values, key=len, reverse=True)

    def text(self, value: str) -> str:
        redacted = value
        for secret in self.secret_values:
            redacted = redacted.replace(secret, "[REDACTED]")

        def replace_inline(match: re.Match) -> str:
            prefix = match.group(1) or match.group(2) or ""
            return prefix + "[REDACTED]"

        return INLINE_SECRET_RE.sub(replace_inline, redacted)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {str(key): self.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


class TraceWriter:
    def __init__(
        self,
        path: Path,
        *,
        task_id: str,
        run_id: str,
        redactor: Optional[Redactor] = None,
    ) -> None:
        self.path = path
        self.task_id = task_id
        self.run_id = run_id
        self.redactor = redactor or Redactor()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, event_type: str, state: str, data: Optional[Dict[str, Any]] = None) -> None:
        payload = {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "task_id": self.task_id,
            "run_id": self.run_id,
            "event_type": event_type,
            "state": state,
            "data": self.redactor.value(data or {}),
        }
        line = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise TraceWriteError("无法写入 Trace：%s" % exc) from exc

    def with_run(self, path: Path, run_id: str) -> "TraceWriter":
        return TraceWriter(
            path,
            task_id=self.task_id,
            run_id=run_id,
            redactor=self.redactor,
        )

