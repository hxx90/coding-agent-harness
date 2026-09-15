"""Content-addressed images and portable, bounded multimodal messages.

Video is represented by timestamped frames. Native video is deliberately not
sent to a provider until that provider's concrete protocol is implemented.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from agent_harness.trace import Redactor

from .types import CodingError, Json

MAX_IMAGE = 2 * 1024 * 1024
MIMES = {"image/png", "image/jpeg", "image/webp"}


def redact_content(content: Any, redactor: Redactor) -> Any:
    if isinstance(content, str):
        return redactor.text(content)
    result = copy.deepcopy(content)
    for part in result or []:
        if part["type"] == "text":
            part["text"] = redactor.text(part["text"])
    return result


def tool_content(outcome: Json, images: list[Json]) -> Any:
    text = json.dumps(outcome, ensure_ascii=False)
    return [{"type": "text", "text": text}, *images] if images else text


def validate_content(content: Any) -> None:
    if content is None or isinstance(content, str):
        return
    if not isinstance(content, list) or len(content) > 16:
        raise ValueError("invalid multimodal content")
    for part in content:
        if not isinstance(part, dict):
            raise TypeError("invalid content part")
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            continue
        if (
            part.get("type") != "image_ref"
            or part.get("mime") not in MIMES
            or not isinstance(part.get("id"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", part["id"])
            or not isinstance(part.get("metadata", {}), dict)
        ):
            raise ValueError("invalid image reference")
        if len(json.dumps(part)) > 32_000:
            raise ValueError("image metadata too large")


def text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        p.get("text", "[image " + p.get("id", "") + "]") for p in content or []
    )


def references(messages: list[Json]) -> list[Json]:
    return [
        part
        for message in messages
        for part in (
            message["content"] if isinstance(message.get("content"), list) else []
        )
        if part.get("type") == "image_ref"
    ]


class MediaStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, data: bytes, mime: str, *, metadata: Json | None = None) -> Json:
        if mime not in MIMES or len(data) > MAX_IMAGE:
            raise CodingError(
                "media_limit", "Supported images are PNG/JPEG/WebP, at most 2 MiB"
            )
        signature = (
            (mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
            or (mime == "image/jpeg" and data.startswith(b"\xff\xd8"))
            or (
                mime == "image/webp"
                and data.startswith(b"RIFF")
                and data[8:12] == b"WEBP"
            )
        )
        if not signature:
            raise CodingError(
                "invalid_media", "Image signature does not match MIME type"
            )
        key = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / key
        try:
            with path.open("xb") as handle:
                path.chmod(0o600)
                handle.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise CodingError("media_integrity", "Existing image hash mismatch")
        ref = {"type": "image_ref", "id": key, "mime": mime, "metadata": metadata or {}}
        validate_content([ref])
        return ref

    def get(self, ref: Json) -> bytes:
        validate_content([ref])
        try:
            path = self.root / ref["id"]
            if path.is_symlink() or path.stat().st_size > MAX_IMAGE:
                raise ValueError("invalid blob")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != ref["id"]:
                raise ValueError("hash mismatch")
            return data
        except (OSError, ValueError) as exc:
            raise CodingError(
                "media_unavailable", f"Missing or invalid media {ref['id']}"
            ) from exc

    def export(self, messages: list[Json]) -> Json:
        return {
            ref["id"]: base64.b64encode(self.get(ref)).decode()
            for ref in references(messages)
        }

    def restore(self, messages: list[Json], blobs: Json) -> None:
        for ref in references(messages):
            if ref["id"] not in blobs:
                self.get(ref)
                continue
            if (
                not isinstance(blobs[ref["id"]], str)
                or len(blobs[ref["id"]]) > (MAX_IMAGE + 2) // 3 * 4
            ):
                raise ValueError("imported image exceeds 2 MiB")
            data = base64.b64decode(blobs[ref["id"]], validate=True)
            if hashlib.sha256(data).hexdigest() != ref["id"]:
                raise ValueError("imported media hash mismatch")
            self.put(data, ref["mime"])

    def provider_messages(self, messages: list[Json]) -> list[Json]:
        """Chat Completions image_url parts; tools remain protocol-correct text.

        Tool images are inserted as a user evidence message after the entire tool
        group. Only the latest eight frames (<= 8 MiB) are transmitted. Metadata
        and omitted-frame notices remain in the text transcript.
        """
        result: list[Json] = []
        selected = {id(ref) for ref in references(messages)[-8:]}
        pending: list[Json] = []
        total = 0
        for index, message in enumerate(messages):
            item = copy.deepcopy(message)
            content = message.get("content")
            if isinstance(content, list):
                parts: list[Json] = []
                for part in content:
                    if part["type"] == "text":
                        parts.append(copy.deepcopy(part))
                        continue
                    parts.append(
                        {
                            "type": "text",
                            "text": "Recorded observation (check timestamp before acting): "
                            + json.dumps(part),
                        }
                    )
                    if id(part) not in selected:
                        continue
                    data = self.get(part)
                    total += len(data)
                    if total > 8 * 1024 * 1024:
                        raise CodingError("media_limit", "Request media exceeds 8 MiB")
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{part['mime']};base64,"
                                + base64.b64encode(data).decode()
                            },
                        }
                    )
                if message["role"] == "tool":
                    item["content"] = text_content(content)
                    pending.extend(
                        [
                            {
                                "type": "text",
                                "text": "Evidence from tool " + message["tool_call_id"],
                            },
                            *parts,
                        ]
                    )
                else:
                    item["content"] = parts
            result.append(item)
            if pending and (
                index + 1 == len(messages) or messages[index + 1]["role"] != "tool"
            ):
                result.append({"role": "user", "content": pending})
                pending = []
        return result
