"""Retry only failed, wholly un-emitted model requests before tool dispatch."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import email.utils
from http import HTTPStatus
import json
import math
import time
from urllib.parse import urlsplit

from vibe.core.agent_loop._generation import GenerationBudgetError, GenerationControl
from vibe.core.llm.exceptions import BackendError
from vibe.core.utils import RetryCategory, RetryObserver, RetryReason

_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 60
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class RetryableRequestError(RuntimeError):
    def __init__(self, error: BackendError, reason: RetryReason) -> None:
        self.error = error
        self.reason = reason
        super().__init__(str(error))


def request_retry_reason(error: BackendError, *, bounded: bool) -> RetryReason | None:
    # Bounded sessions disable backend retries: every attempt must be reserved
    # and audited by the generation controller. Other sessions retain backend
    # retry ownership for standard transport/status errors, avoiding nesting.
    if bounded:
        if error.status in _TRANSIENT_STATUS:
            assert error.status is not None
            return RetryReason.from_http_status(error.status)
        if error.status is None and error.parsed_error == "Network error":
            return RetryReason(RetryCategory.CONNECTION, "Model request network error")
    return _transient_channel_reason(error)


def _transient_channel_reason(error: BackendError) -> RetryReason | None:
    if (
        error.status != HTTPStatus.FORBIDDEN
        or urlsplit(error.endpoint).hostname != "api-gateway.glm.ai"
    ):
        return None
    try:
        body = json.loads(error.body_text)
    except ValueError:
        return None
    detail = body.get("error") if isinstance(body, dict) else None
    if (
        not isinstance(detail, dict)
        or detail.get("code") != "get_channel_failed"
        or not isinstance(detail.get("message"), str)
    ):
        return None
    message = detail["message"]
    # This gateway can wrap mixed upstream failures as 403. Only explicit
    # transient channel evidence permits retries; ordinary auth/quota/IP errors
    # remain terminal. No credential, endpoint or channel is changed.
    if any(
        s in message.lower()
        for s in ("statuscode: 429", "throttlingexception", "请求速率限制")
    ):
        return RetryReason(
            RetryCategory.RATE_LIMITED, "Gateway transient channel failure (HTTP 403)"
        )
    if any(
        s in message.lower()
        for s in ("statuscode: 503", "temporarily unavailable", "service unavailable")
    ):
        return RetryReason(
            RetryCategory.SERVER_ERROR, "Gateway transient channel failure (HTTP 403)"
        )
    return None


def retry_delay(error: BackendError, attempt: int) -> float:
    delay = min(2.0 ** min(attempt, 6), float(_MAX_BACKOFF_SECONDS))
    value = error.headers.get("retry-after", "").strip()
    if value.isascii() and value.isdigit():
        return max(delay, float(value))
    if value:
        try:
            date = email.utils.parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            delay = max(delay, (date - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return delay


async def wait_for_request_retry(
    error: RetryableRequestError,
    control: GenerationControl,
    attempt: int,
    notice: RetryObserver,
) -> None:
    continuous = control.config.enabled and not control.config.limits_enabled
    if not continuous and attempt > _MAX_RETRIES:
        control.audit(
            "request_retry_exhausted", reason="attempt_limit", retries=_MAX_RETRIES
        )
        raise error.error
    delay = retry_delay(error.error, attempt)
    if not math.isfinite(delay) or (not continuous and delay > _MAX_BACKOFF_SECONDS):
        control.audit("request_retry_exhausted", reason="retry_after_too_long")
        raise error.error
    if control.config.limits_enabled and (
        control.calls >= control.config.max_calls
        or (
            control.deadline is not None
            and time.monotonic() + delay >= control.deadline
        )
    ):
        control.audit(
            "request_retry_exhausted", reason="shared_budget", calls=control.calls
        )
        raise GenerationBudgetError("Shared generation budget exhausted before retry")
    control.audit(
        "request_retry",
        retry=attempt,
        delay_seconds=delay,
        after_call=control.calls,
        status=error.error.status,
        category=error.reason.category,
        reason=error.reason.detail,
    )
    await notice(error.reason)
    await asyncio.sleep(delay)  # cancellation propagates; no request is sent on cancel
