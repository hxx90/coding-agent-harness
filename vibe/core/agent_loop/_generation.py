"""Request reservations and audit for bounded real-CLI robot trials.

Input reservations use UTF-8 bytes (including tool schemas and image data),
plus framing allowance. This deliberately overestimates text tokenization;
local RGB images reserve an additional token per pixel (minimum 4096).
Provider-reported overages fail closed before dispatch.
Missing usage consumes the entire reservation, never zero.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
import time
from typing import Any

from vibe.core.config.models import GenerationConfig
from vibe.core.types import AvailableTool, LLMChunk, LLMMessage


class GenerationBudgetError(RuntimeError):
    pass


class GenerationControl:
    def __init__(self, config: GenerationConfig) -> None:
        self.config = config
        self.calls = 0
        self.input_charged = 0
        self.output_charged = 0
        self.deadline: float | None = None
        self.recovery_used = False

    def audit(self, kind: str, **data: Any) -> None:
        if not self.config.enabled or not self.config.audit_dir:
            return
        path = Path(self.config.audit_dir)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "generation.jsonl").open("a", encoding="utf-8") as out:
            out.write(
                json.dumps(
                    {"kind": kind, "time": time.time(), **data}, ensure_ascii=False
                )
                + "\n"
            )

    def start(self) -> None:
        if self.config.enabled and self.deadline is None:
            self.deadline = time.monotonic() + self.config.max_seconds

    def reserve(
        self,
        messages: Sequence[LLMMessage],
        tools: list[AvailableTool] | None,
        requested: int | None,
    ) -> tuple[int | None, int]:
        if not self.config.enabled:
            return requested, 0
        self.start()
        if not self.config.limit_tokens:
            if self.calls >= self.config.max_calls or (
                self.deadline is not None and time.monotonic() >= self.deadline
            ):
                self.audit("budget_exhausted", calls=self.calls)
                raise GenerationBudgetError(
                    "Generation task budget exhausted before request"
                )
            self.calls += 1
            self.audit(
                "request",
                call=self.calls,
                limit_tokens=False,
                input_reserved=0,
                max_tokens=requested,
                messages=[
                    m.model_dump(mode="json", exclude_none=True) for m in messages
                ],
                tools=[t.model_dump(mode="json") for t in tools or []],
            )
            # No reservation or implicit cap. An explicit caller limit still applies.
            return requested, 0
        image_reservation = 0
        for message in messages:
            for attachment in message.images or []:
                import base64
                import io

                from PIL import Image

                source = attachment.source
                data = (
                    Path(source.path).read_bytes()
                    if source.kind == "file"
                    else base64.b64decode(source.data)
                )
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
                image_reservation += max(4096, width * height) + len(data)
        payload = {
            "messages": [
                m.model_dump(mode="json", exclude_none=True) for m in messages
            ],
            "tools": [t.model_dump(mode="json") for t in tools or []],
        }
        reservation = (
            len(json.dumps(payload, ensure_ascii=False).encode())
            + 4096
            + 256 * len(messages)
            + image_reservation
        )
        remaining_output = self.config.max_output_tokens - self.output_charged
        if (
            self.calls >= self.config.max_calls
            or self.input_charged + reservation > self.config.max_input_tokens
            or remaining_output <= 0
            or (self.deadline is not None and time.monotonic() >= self.deadline)
        ):
            self.audit(
                "budget_exhausted",
                calls=self.calls,
                input_charged=self.input_charged,
                output_charged=self.output_charged,
            )
            raise GenerationBudgetError(
                "Generation task budget exhausted before request"
            )
        output = min(
            requested or self.config.per_call_output_tokens,
            self.config.per_call_output_tokens,
            remaining_output,
        )
        self.calls += 1
        self.input_charged += reservation
        self.output_charged += output
        self.audit(
            "request",
            call=self.calls,
            input_reserved=reservation,
            max_tokens=output,
            **payload,
        )
        return output, reservation

    def finish(
        self,
        chunk: LLMChunk | None,
        reservation: int,
        output: int | None,
        error: str | None = None,
    ) -> None:
        if not self.config.enabled:
            return
        if not self.config.limit_tokens:
            # These totals include only provider-reported usage, not reservations.
            if chunk is not None and chunk.usage is not None:
                self.input_charged += chunk.usage.prompt_tokens
                self.output_charged += chunk.usage.completion_tokens
            self.audit(
                "response",
                call=self.calls,
                response=chunk.model_dump(mode="json", exclude_none=True)
                if chunk
                else None,
                error=error,
                limit_tokens=False,
                usage_missing=chunk is None or chunk.usage is None,
                input_reported=self.input_charged,
                output_reported=self.output_charged,
            )
            return
        # Keep input reservations charged: provider usage is evidence, not a
        # tokenizer guarantee. Output usage is exact only for complete responses.
        violation = False
        if error is None and chunk is not None and chunk.usage is not None:
            if (
                chunk.usage.prompt_tokens > reservation
                or chunk.usage.completion_tokens > (output or 0)
            ):
                violation = True
            else:
                self.output_charged -= (output or 0) - chunk.usage.completion_tokens
        self.audit(
            "response",
            call=self.calls,
            response=chunk.model_dump(mode="json", exclude_none=True)
            if chunk
            else None,
            error=error,
            input_charged=self.input_charged,
            output_charged=self.output_charged,
        )

        if violation:
            self.audit("provider_budget_violation")
            raise GenerationBudgetError("Provider exceeded the reserved token bound")


class IncompleteGenerationError(RuntimeError):
    """A rejected, wholly undispatched model generation."""


def validate_generation(chunk: LLMChunk, *, require_reason: bool) -> None:
    reason = chunk.stop.reason if chunk.stop else None
    if reason in {"length", "max_tokens", "max_output_tokens", "incomplete"}:
        raise IncompleteGenerationError(f"truncated:{reason}")
    if reason is None and require_reason:
        raise IncompleteGenerationError("missing_finish_reason")
    if reason is not None and reason not in {
        "stop",
        "tool_calls",
        "end_turn",
        "tool_use",
        "refusal",
    }:
        raise IncompleteGenerationError("unrecognized_finish_reason")
    for call in chunk.message.tool_calls or []:
        try:
            args = json.loads(call.function.arguments or "")
        except (json.JSONDecodeError, TypeError) as exc:
            raise IncompleteGenerationError("incomplete_tool_arguments") from exc
        if not isinstance(args, dict) or not call.id or not call.function.name:
            raise IncompleteGenerationError("invalid_tool_envelope")
