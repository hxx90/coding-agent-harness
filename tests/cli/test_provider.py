from __future__ import annotations

import threading
import time

import pytest
from conftest import Reply, answer, call, sse

from agent_harness.coding.provider import Provider
from agent_harness.coding.settings import ProviderSettings
from agent_harness.coding.types import CodingError


def provider(model_server, responses, **kwargs):
    url, requests, headers = model_server(responses)
    return (
        Provider(ProviderSettings(base_url=url, model="test-model", **kwargs)),
        requests,
        headers,
    )


def complete(client, stop=None, chunks=None):
    return client.complete(
        [{"role": "user", "content": "Hello"}],
        [],
        on_text=(chunks if chunks is not None else []).append,
        stop=stop or threading.Event(),
    )


def test_json_fallback_usage_metadata_and_models(model_server):
    client, requests, headers = provider(
        model_server,
        [answer("Hello", [call("read", {"path": "x"})], reasoning_content="private")],
        api_key="test-key",
    )
    chunks = []
    result = complete(client, chunks=chunks)
    assert result.text == "Hello"
    assert result.calls[0].name == "read"
    assert result.extra == {"reasoning_content": "private"}
    assert result.usage["total_tokens"] == 15
    assert chunks == ["Hello"]
    assert headers[0]["Authorization"] == "Bearer test-key"
    assert requests[0]["stream"] is True
    assert client.models() == ["test-model"]


def test_stream_assembles_text_tool_arguments_and_usage(model_server):
    stream = sse(
        {
            "choices": [
                {
                    "delta": {
                        "content": "你",
                        "reasoning_content": "private",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "abc",
                                "function": {"name": "re", "arguments": '{"path":'},
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "content": "好",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "abc",
                                "function": {"name": "ad", "arguments": '"a.py"}'},
                            }
                        ],
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"total_tokens": 20}},
        "[DONE]",
    )
    client, _, _ = provider(model_server, [stream])
    chunks = []
    result = complete(client, chunks=chunks)
    assert chunks == ["你", "好"]
    assert result.calls[0].id == "abc"
    assert result.calls[0].name == "read"
    assert result.calls[0].arguments == '{"path":"a.py"}'
    assert result.extra["reasoning_content"] == "private"
    assert result.usage == {"total_tokens": 20}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"message": 4}]},
        answer(calls=[call("read", {}, "x"), call("read", {}, "x")]),
    ],
)
def test_invalid_responses_are_actionable(model_server, payload):
    client, _, _ = provider(model_server, [payload])
    with pytest.raises(CodingError) as exc:
        complete(client)
    assert exc.value.code == "invalid_model_response"


def test_incomplete_stream_retains_partial_without_retry(model_server):
    client, requests, _ = provider(
        model_server, [sse({"choices": [{"delta": {"content": "partial"}}]}, "[DONE]")]
    )
    chunks = []
    with pytest.raises(CodingError) as exc:
        complete(client, chunks=chunks)
    assert exc.value.code == "incomplete_stream"
    assert chunks == ["partial"]
    assert len(requests) == 1


def test_only_transient_errors_retry_and_credentials_are_redacted(model_server):
    client, requests, _ = provider(
        model_server, [Reply(status=503, body="busy"), answer("recovered")]
    )
    assert complete(client).text == "recovered"
    assert len(requests) == 2
    client, requests, _ = provider(
        model_server,
        [Reply(status=401, body="super-secret-value")],
        api_key="super-secret-value",
    )
    with pytest.raises(CodingError) as exc:
        complete(client)
    assert exc.value.code == "authentication"
    assert "super-secret-value" not in str(exc.value)
    assert len(requests) == 1


def test_negotiates_and_retains_max_completion_tokens_when_provider_requires_it(
    model_server,
):
    client, requests, _ = provider(
        model_server,
        [
            Reply(
                status=400,
                body={
                    "error": {
                        "message": (
                            "Unsupported parameter: 'max_tokens' is not supported "
                            "with this model. Use 'max_completion_tokens' instead."
                        ),
                        "param": "max_tokens",
                        "code": "unsupported_parameter",
                    }
                },
            ),
            answer("compatible"),
            answer("cached"),
        ],
        max_output_tokens=321,
    )

    assert complete(client).text == "compatible"
    assert requests[0]["max_tokens"] == 321
    assert "max_completion_tokens" not in requests[0]
    assert requests[1]["max_completion_tokens"] == 321
    assert "max_tokens" not in requests[1]
    assert complete(client).text == "cached"
    assert requests[2]["max_completion_tokens"] == 321
    assert "max_tokens" not in requests[2]


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_cancel_interrupts_real_http_without_waiting_for_timeout(model_server, phase):
    reply = (
        Reply(body=answer(), delay_headers=3)
        if phase == "headers"
        else Reply(
            chunks=[b'data: {"choices":[]}\n\n', b"data: [DONE]\n\n"],
            delay_chunks=3,
            content_type="text/event-stream",
        )
    )
    client, _, _ = provider(model_server, [reply], timeout=20)
    stop = threading.Event()
    timer = threading.Timer(0.2, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(CodingError) as exc:
            complete(client, stop=stop)
    finally:
        timer.cancel()
    assert exc.value.code == "cancelled"
    assert time.monotonic() - started < 1.5


def test_retry_budget_is_finite(model_server):
    client, requests, _ = provider(model_server, [Reply(status=429)] * 3)
    with pytest.raises(CodingError):
        complete(client)
    assert len(requests) == 3


def test_stream_limit_includes_lines_without_newlines(model_server):
    client, _, _ = provider(
        model_server,
        [
            Reply(
                chunks=[b"x" * (2 * 1024 * 1024 + 1)], content_type="text/event-stream"
            )
        ],
    )
    with pytest.raises(CodingError) as exc:
        complete(client)
    assert exc.value.code == "response_too_large"
