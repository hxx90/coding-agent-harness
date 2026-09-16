from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import aclosing

from vibe.app_server.events import CallbackRequested
from vibe.app_server.local import LocalHarness, LocalHarnessOptions
from vibe.app_server.models import (
    ImageAttachment,
    InlineImageSource,
    PublicHistoryEntry,
    PublicMessageEntry,
)
from vibe.capx._agent_port import CapXHarnessSession, CapXSessionFactory
from vibe.capx.app import parse_image_data_url
from vibe.capx.models import (
    CapXCompletionRequest,
    CapXImagePart,
    CapXMessage,
    CapXTextPart,
)


class CapXHarnessAgent:
    def __init__(
        self, start_session: CapXSessionFactory, *, max_concurrency: int = 1
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        self._start_session = start_session
        self._semaphore = asyncio.Semaphore(max_concurrency)

    @classmethod
    def local(
        cls, options: LocalHarnessOptions, *, max_concurrency: int = 1
    ) -> CapXHarnessAgent:
        async def start_session() -> CapXHarnessSession:
            return await LocalHarness(options).start()

        return cls(start_session, max_concurrency=max_concurrency)

    async def complete(self, request: CapXCompletionRequest, run_id: str) -> str:
        prompt, images = prepare_harness_input(request, run_id=run_id)
        async with self._semaphore:
            session = await self._start_session()
            try:
                async with aclosing(session.act(prompt, images=images)) as events:
                    async for event in events:
                        if isinstance(event, CallbackRequested):
                            await session.deny_callback(event.callback)
                response = _last_assistant_text(session.history)
                if response is None:
                    raise RuntimeError("Robo returned no assistant response")
                return response
            finally:
                await session.close()


def prepare_harness_input(
    request: CapXCompletionRequest, *, run_id: str
) -> tuple[str, list[ImageAttachment]]:
    sections = [
        "You are Robo running as the coding agent inside CaP-X Bench.",
        "Follow the conversation below and return only the answer CaP-X requested. "
        "When it asks for code, preserve its exact fenced-Python response format.",
        f"Robo run_id: {run_id}",
    ]
    images: list[ImageAttachment] = []
    for message in request.messages:
        text, message_images = _message_content(message, start_index=len(images))
        sections.append(f"[{message.role}]\n{text}")
        images.extend(message_images)
    return "\n\n".join(sections), images


def _message_content(
    message: CapXMessage, *, start_index: int
) -> tuple[str, list[ImageAttachment]]:
    if isinstance(message.content, str):
        return message.content, []
    text_parts: list[str] = []
    images: list[ImageAttachment] = []
    for part in message.content:
        match part:
            case CapXTextPart(text=text):
                text_parts.append(text)
            case CapXImagePart(image_url=image_url):
                mime_type, encoded = parse_image_data_url(image_url.url)
                index = start_index + len(images) + 1
                text_parts.append(f"[image {index} attached]")
                images.append(
                    ImageAttachment(
                        source=InlineImageSource(data=encoded),
                        alias=f"capx-image-{index}",
                        mime_type=mime_type,
                    )
                )
    return "\n".join(text_parts), images


def _last_assistant_text(history: Sequence[PublicHistoryEntry]) -> str | None:
    return next(
        (
            entry.text
            for entry in reversed(history)
            if isinstance(entry, PublicMessageEntry)
            and entry.role == "assistant"
            and entry.text
        ),
        None,
    )


__all__ = [
    "CapXHarnessAgent",
    "CapXHarnessSession",
    "CapXSessionFactory",
    "prepare_harness_input",
]
