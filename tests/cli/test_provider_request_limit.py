import threading

import pytest
from conftest import Reply, answer

from agent_harness.coding.provider import Provider
from agent_harness.coding.settings import ProviderSettings
from agent_harness.coding.types import CodingError


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_request_limit_validation(limit):
    with pytest.raises(CodingError, match="positive integer"):
        Provider(ProviderSettings(request_limit=limit))


def test_http_retries_share_the_hard_request_limit(model_server):
    url, requests, _ = model_server(
        [Reply(status=503, body={"error": "busy"}), answer()]
    )
    provider = Provider(ProviderSettings(base_url=url, model="test", request_limit=1))
    with pytest.raises(CodingError) as exc:
        provider.complete(
            [{"role": "user", "content": "test"}],
            [],
            on_text=lambda _: None,
            stop=threading.Event(),
        )
    assert exc.value.code == "budget_exhausted"
    assert len(requests) == provider.request_count == 1


def test_request_limit_spans_successive_completions(model_server):
    url, requests, _ = model_server([answer(), answer()])
    provider = Provider(ProviderSettings(base_url=url, model="test", request_limit=1))
    arguments = {
        "messages": [{"role": "user", "content": "test"}],
        "tools": [],
        "on_text": lambda _: None,
        "stop": threading.Event(),
    }
    provider.complete(**arguments)
    with pytest.raises(CodingError, match="request limit"):
        provider.complete(**arguments)
    assert len(requests) == provider.request_count == 1
