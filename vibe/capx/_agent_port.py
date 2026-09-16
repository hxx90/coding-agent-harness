from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from typing import Any, Protocol

from vibe.app_server.models import (
    ImageAttachment,
    PublicCallbackEntry,
    PublicHistoryEntry,
)
from vibe.capx.models import CapXCompletionRequest


class CapXCompletionAgent(Protocol):
    async def complete(self, request: CapXCompletionRequest, run_id: str) -> str: ...


class CapXHarnessSession(Protocol):
    @property
    def history(self) -> Sequence[PublicHistoryEntry]: ...

    def act(
        self, message: str, *, images: list[ImageAttachment] | None = None
    ) -> AsyncGenerator[Any, None]: ...

    async def deny_callback(self, callback: PublicCallbackEntry) -> None: ...

    async def close(self) -> Any: ...


type CapXSessionFactory = Callable[[], Awaitable[CapXHarnessSession]]


__all__ = ["CapXCompletionAgent", "CapXHarnessSession", "CapXSessionFactory"]
