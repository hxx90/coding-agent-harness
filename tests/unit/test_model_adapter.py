from __future__ import annotations

import io
import urllib.error

import pytest

from agent_harness.errors import ModelAPIError
from agent_harness.model_adapter import OpenAICompatibleAdapter


def test_http_error_details_redact_ui_api_key(monkeypatch):
    api_key = "dummy"
    adapter = OpenAICompatibleAdapter(
        base_url="https://model.invalid/v1",
        api_key=api_key,
        model="test-model",
    )
    error = urllib.error.HTTPError(
        adapter.url,
        400,
        "bad request",
        {},
        io.BytesIO(("server echoed " + api_key).encode("utf-8")),
    )

    def raise_http_error(*args, **kwargs):
        raise error

    monkeypatch.setattr("urllib.request.urlopen", raise_http_error)
    with pytest.raises(ModelAPIError) as caught:
        adapter.complete([], [], timeout_seconds=1)

    response = caught.value.details["response"]
    assert api_key not in response
    assert "[REDACTED]" in response


def test_transient_504_is_tried_three_times_and_reports_attempts(monkeypatch):
    adapter = OpenAICompatibleAdapter(
        base_url="https://model.invalid/v1",
        api_key="dummy",
        model="test-model",
    )
    calls = []

    def raise_gateway_timeout(*args, **kwargs):
        calls.append(1)
        raise urllib.error.HTTPError(
            adapter.url,
            504,
            "gateway timeout",
            {},
            io.BytesIO(b"temporarily unavailable"),
        )

    monkeypatch.setattr("urllib.request.urlopen", raise_gateway_timeout)
    monkeypatch.setattr("agent_harness.model_adapter.time.sleep", lambda _: None)

    with pytest.raises(ModelAPIError) as caught:
        adapter.complete([], [], timeout_seconds=1)

    assert len(calls) == 3
    assert caught.value.code == "model_service_error"
    assert "已自动尝试 3 次" in caught.value.message
    assert caught.value.details["status"] == 504
    assert caught.value.details["attempts"] == 3


def test_lists_gateway_models_without_sending_a_completion(monkeypatch):
    adapter = OpenAICompatibleAdapter(
        base_url="https://token.example/v1",
        api_key="dummy",
        model="MiniMax-M3",
    )
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b'{"data":[{"id":"other"},{"id":"MiniMax-M3"}]}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert adapter.list_models() == ["MiniMax-M3", "other"]
    assert captured["url"] == "https://token.example/v1/models"
    assert captured["authorization"] == "Bearer dummy"
    assert adapter.name == "openai-compatible/MiniMax-M3"


def test_normalize_preserves_minimax_reasoning_metadata_for_next_turn():
    response = OpenAICompatibleAdapter._normalize(
        {
            "id": "response-1",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_details": [{"type": "text", "text": "opaque"}],
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "list_files",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                }
            ],
        }
    )
    assert response.provider_message_fields == {
        "reasoning_details": [{"type": "text", "text": "opaque"}]
    }


def test_minimax_concatenated_tool_argument_objects_are_merged():
    assert OpenAICompatibleAdapter._tool_arguments(
        '{}{"directory":"src"}{"max_results":10}',
        "list_files",
    ) == {"directory": "src", "max_results": 10}


def test_invalid_tool_arguments_report_stage_and_preview():
    with pytest.raises(ModelAPIError) as caught:
        OpenAICompatibleAdapter._tool_arguments(
            '{} trailing prose',
            "list_files",
        )
    assert caught.value.recoverable
    assert caught.value.details["stage"] == "tool_arguments"
    assert caught.value.details["tool_name"] == "list_files"
    assert caught.value.details["arguments_preview"] == '{} trailing prose'
