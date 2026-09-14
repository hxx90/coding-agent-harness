"""Bounded, cancellable Chat Completions HTTP/SSE transport."""

from __future__ import annotations

import asyncio
import codecs
import json
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, Protocol, TypeVar

import httpx

from agent_harness.trace import Redactor

from .settings import ProviderSettings
from .types import Cancelled, CodingError, Completion, Json, ToolCall

MAX_RESPONSE_CHARS = 2 * 1024 * 1024
T = TypeVar("T")


class Model(Protocol):
    def complete(
        self,
        messages: list[Json],
        tools: list[Json],
        *,
        on_text: Callable[[str], None],
        stop: threading.Event,
    ) -> Completion: ...


async def interruptible(operation: Coroutine[Any, Any, T], stop: threading.Event) -> T:
    """Cancel the async I/O task even during connection establishment or headers."""
    task = asyncio.create_task(operation)
    try:
        while not task.done():
            if stop.is_set():
                task.cancel()
                raise Cancelled()
            await asyncio.wait({task}, timeout=0.05)
        if stop.is_set():
            raise Cancelled()
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class Provider:
    def __init__(self, settings: ProviderSettings) -> None:
        self.settings = settings
        self.redactor = Redactor([settings.api_key] if settings.api_key else [])

    def _headers(self) -> dict[str, str]:
        result = {"Accept": "application/json, text/event-stream"}
        if self.settings.api_key:
            result["Authorization"] = "Bearer " + self.settings.api_key
        return result

    def _configured(self, *, model: bool = True) -> None:
        if not self.settings.base_url or (model and not self.settings.model):
            raise CodingError(
                "configuration",
                "Configure --base-url/--model or AGENT_HARNESS_BASE_URL/AGENT_HARNESS_MODEL",
            )

    @staticmethod
    async def _body(response: httpx.Response, limit: int = MAX_RESPONSE_CHARS) -> bytes:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > limit:
                raise CodingError(
                    "response_too_large", f"Model response exceeds {limit} bytes"
                )
            data.extend(chunk)
        return bytes(data)

    async def _check_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk[: 2000 - len(data)])
            if len(data) >= 2000:
                break
        code = (
            "authentication" if response.status_code in {401, 403} else "provider_http"
        )
        raise CodingError(
            code,
            self.redactor.text(
                f"Model service returned HTTP {response.status_code}: {data.decode('utf-8', errors='replace')}"
            ),
            details={"status": response.status_code},
        )

    def models(self, stop: threading.Event | None = None) -> list[str]:
        self._configured(model=False)

        async def fetch() -> list[str]:
            async with (
                httpx.AsyncClient(
                    timeout=self.settings.timeout, follow_redirects=False
                ) as client,
                client.stream(
                    "GET", self.settings.base_url + "/models", headers=self._headers()
                ) as response,
            ):
                await self._check_status(response)
                data = json.loads(await self._body(response))
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise CodingError(
                    "invalid_model_response", "Model list must have a data array"
                )
            return sorted(
                {
                    str(item["id"])
                    for item in data["data"]
                    if isinstance(item, dict) and item.get("id")
                }
            )

        try:
            return asyncio.run(interruptible(fetch(), stop or threading.Event()))
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise CodingError("provider_error", self.redactor.text(str(exc))) from exc

    @staticmethod
    def _content(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(
                str(item.get("text", "")) for item in value if isinstance(item, dict)
            )
        raise ValueError("assistant content must be text")

    @staticmethod
    def _validate(result: Completion) -> Completion:
        ids = [call.id for call in result.calls]
        if len(set(ids)) != len(ids):
            raise CodingError("invalid_model_response", "Duplicate tool call IDs")
        if not isinstance(result.usage, dict):
            raise CodingError("invalid_model_response", "usage must be an object")
        return result

    @classmethod
    def _normalize(cls, payload: Any) -> Completion:
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            calls = []
            for raw in message.get("tool_calls") or []:
                function = raw["function"]
                arguments = function.get("arguments", "{}")
                if isinstance(arguments, dict):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                if not isinstance(arguments, str) or not isinstance(
                    function["name"], str
                ):
                    raise TypeError("invalid tool arguments/name")
                calls.append(
                    ToolCall(
                        str(raw.get("id") or "call_" + uuid.uuid4().hex),
                        function["name"],
                        arguments,
                    )
                )
            return cls._validate(
                Completion(
                    cls._content(message.get("content")),
                    calls,
                    payload.get("usage") or {},
                    choice.get("finish_reason") or "stop",
                    {
                        key: message[key]
                        for key in ("reasoning_content", "reasoning_details")
                        if message.get(key) is not None
                    },
                )
            )
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise CodingError(
                "invalid_model_response", f"Invalid Chat Completions response: {exc}"
            ) from exc

    @staticmethod
    async def _sse(response: httpx.Response) -> AsyncIterator[str]:
        # Count bytes before splitting lines, including a server sending no newline.
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        pending = ""
        data: list[str] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_CHARS:
                raise CodingError("response_too_large", "Model stream exceeds 2 MiB")
            pending += decoder.decode(chunk)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                line = line.rstrip("\r")
                if not line and data:
                    yield "\n".join(data)
                    data.clear()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
        pending += decoder.decode(b"", final=True)
        if pending.startswith("data:"):
            data.append(pending[5:].strip())
        if data:
            yield "\n".join(data)

    async def _stream(
        self, response: httpx.Response, on_text: Callable[[str], None]
    ) -> Completion:
        text: list[str] = []
        calls: dict[int, Json] = {}
        usage: Json = {}
        extra: Json = {}
        finish = ""
        try:
            async for raw in self._sse(response):
                if raw == "[DONE]":
                    break
                payload = json.loads(raw)
                if payload.get("error"):
                    raise CodingError(
                        "provider_error", self.redactor.text(str(payload["error"]))
                    )
                if payload.get("usage"):
                    usage = payload["usage"]
                choices = payload.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})
                piece = self._content(delta.get("content"))
                if piece:
                    text.append(piece)
                    on_text(piece)
                for key in ("reasoning_content", "reasoning_details"):
                    value = delta.get(key)
                    if isinstance(value, str):
                        extra[key] = str(extra.get(key, "")) + value
                    elif isinstance(value, list):
                        extra.setdefault(key, []).extend(value)
                for raw_call in delta.get("tool_calls") or []:
                    index = int(raw_call.get("index", 0))
                    if index < 0 or index >= 128:
                        raise ValueError("tool index must be 0..127")
                    call = calls.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    if raw_call.get("id"):
                        if not call["id"]:
                            call["id"] = raw_call["id"]
                        elif call["id"] != raw_call["id"]:
                            raise ValueError("tool ID changed during stream")
                    function = raw_call.get("function", {})
                    for key in ("name", "arguments"):
                        if function.get(key):
                            call[key] += function[key]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            if not finish:
                raise CodingError(
                    "incomplete_stream",
                    "Model stream ended before finish_reason; partial text is retained",
                )
            result_calls = []
            for _, call in sorted(calls.items()):
                if not call["name"]:
                    raise ValueError("tool call has no name")
                result_calls.append(
                    ToolCall(
                        call["id"] or "call_" + uuid.uuid4().hex,
                        call["name"],
                        call["arguments"],
                    )
                )
            return self._validate(
                Completion("".join(text), result_calls, usage, finish, extra)
            )
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CodingError(
                "invalid_model_response", f"Invalid streaming response: {exc}"
            ) from exc

    def complete(
        self,
        messages: list[Json],
        tools: list[Json],
        *,
        on_text: Callable[[str], None],
        stop: threading.Event,
    ) -> Completion:
        self._configured()
        return asyncio.run(
            interruptible(self._complete(messages, tools, on_text), stop)
        )

    async def _complete(
        self, messages: list[Json], tools: list[Json], on_text: Callable[[str], None]
    ) -> Completion:
        payload: Json = {
            "model": self.settings.model,
            "messages": messages,
            "stream": self.settings.stream,
            "max_tokens": self.settings.max_output_tokens,
        }
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if self.settings.temperature is not None:
            payload["temperature"] = self.settings.temperature
        if self.settings.stream:
            payload["stream_options"] = {"include_usage": True}
        for attempt in range(3):
            emitted = False

            def relay(piece: str) -> None:
                nonlocal emitted
                emitted = True
                on_text(piece)

            try:
                async with (
                    httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            self.settings.timeout,
                            connect=min(10, self.settings.timeout),
                        ),
                        follow_redirects=False,
                    ) as client,
                    client.stream(
                        "POST",
                        self.settings.base_url + "/chat/completions",
                        json=payload,
                        headers=self._headers(),
                    ) as response,
                ):
                    await self._check_status(response)
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        return await self._stream(response, relay)
                    result = self._normalize(json.loads(await self._body(response)))
                    if result.text:
                        relay(result.text)
                    return result
            except CodingError as exc:
                status = exc.details.get("status", 0)
                if emitted or attempt == 2 or not (status == 429 or status >= 500):
                    raise
            except (httpx.HTTPError, OSError, ValueError) as exc:
                if emitted or attempt == 2 or isinstance(exc, ValueError):
                    raise CodingError(
                        "provider_error", self.redactor.text(str(exc))
                    ) from exc
            await asyncio.sleep(0.25 * (2**attempt))
        raise CodingError("provider_error", "Model request failed after three attempts")
