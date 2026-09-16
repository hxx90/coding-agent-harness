from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from uuid import uuid4

import anyio
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from vibe.capx._agent_port import CapXCompletionAgent
from vibe.capx.models import CapXCompletionRequest, CapXImagePart
from vibe.observability.logging import logger

_DATA_URL = re.compile(
    r"^data:(?P<mime>image/(?:png|jpeg|webp));base64,(?P<data>[A-Za-z0-9+/=\s]+)$"
)
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGES = 8


def create_capx_app(
    agent: CapXCompletionAgent, *, trace_dir: Path, api_key: str | None = None
) -> Starlette:
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok", "service": "robo-capx-bridge"})

    async def chat_completions(request: Request) -> Response:
        if api_key is not None and request.headers.get("authorization") != (
            f"Bearer {api_key}"
        ):
            return _error(
                "Invalid API key", status=401, error_type="authentication_error"
            )
        try:
            payload = await request.json()
            completion = CapXCompletionRequest.model_validate(payload)
            _validate_images(completion)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _error(str(exc), status=400, error_type="invalid_request_error")
        if completion.stream:
            return _error(
                "Streaming is not supported by the Robo CaP-X bridge",
                status=400,
                error_type="invalid_request_error",
            )

        run_id = uuid4().hex
        trial_id = request.headers.get("x-capx-trial-id") or completion.user
        await _append_trace(
            trace_dir,
            run_id,
            {
                "kind": "request_received",
                "run_id": run_id,
                "trial_id": trial_id,
                "model": completion.model,
                "message_count": len(completion.messages),
                "image_count": _image_count(completion),
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        try:
            content = await agent.complete(completion, run_id)
        except Exception as exc:
            logger.exception("CaP-X completion failed run_id=%s", run_id)
            await _append_trace(
                trace_dir,
                run_id,
                {
                    "kind": "completion_failed",
                    "run_id": run_id,
                    "error": type(exc).__name__,
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )
            return _error(
                "Robo completion failed", status=500, error_type="server_error"
            )

        await _append_trace(
            trace_dir,
            run_id,
            {
                "kind": "completion_created",
                "run_id": run_id,
                "output_chars": len(content),
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        body = {
            "id": f"chatcmpl-{run_id}",
            "object": "chat.completion",
            "created": int(datetime.now(UTC).timestamp()),
            "model": completion.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return JSONResponse(body, headers={"x-robo-run-id": run_id})

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/chat/completions", chat_completions, methods=["POST"]),
        ]
    )


def parse_image_data_url(url: str) -> tuple[str, str]:
    match = _DATA_URL.fullmatch(url)
    if match is None:
        raise ValueError("CaP-X images must use an inline image data URL")
    encoded = re.sub(r"\s+", "", match.group("data"))
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("CaP-X image data URL contains invalid base64") from exc
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError("CaP-X image exceeds the 10 MiB limit")
    return match.group("mime"), encoded


def _validate_images(request: CapXCompletionRequest) -> None:
    image_count = 0
    for message in request.messages:
        if isinstance(message.content, str):
            continue
        for part in message.content:
            if not isinstance(part, CapXImagePart):
                continue
            image_count += 1
            parse_image_data_url(part.image_url.url)
    if image_count > _MAX_IMAGES:
        raise ValueError(f"CaP-X request contains more than {_MAX_IMAGES} images")


def _image_count(request: CapXCompletionRequest) -> int:
    return sum(
        isinstance(part, CapXImagePart)
        for message in request.messages
        if isinstance(message.content, list)
        for part in message.content
    )


def _error(message: str, *, status: int, error_type: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": error_type, "code": None}},
        status_code=status,
    )


async def _append_trace(
    trace_dir: Path, run_id: str, payload: dict[str, object]
) -> None:
    directory = anyio.Path(trace_dir)
    await directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.jsonl"
    async with await path.open("a", encoding="utf-8") as stream:
        await stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


__all__ = ["create_capx_app", "parse_image_data_url"]
