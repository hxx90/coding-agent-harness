from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class CapXImageURL(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    detail: str | None = None


class CapXTextPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["text"]
    text: str


class CapXImagePart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["image_url"]
    image_url: CapXImageURL


CapXContentPart = Annotated[CapXTextPart | CapXImagePart, Field(discriminator="type")]


class CapXMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[CapXContentPart]


class CapXCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[CapXMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    reasoning_effort: str | None = None
    stream: bool = False
    user: str | None = None


__all__ = [
    "CapXCompletionRequest",
    "CapXContentPart",
    "CapXImagePart",
    "CapXImageURL",
    "CapXMessage",
    "CapXTextPart",
]
