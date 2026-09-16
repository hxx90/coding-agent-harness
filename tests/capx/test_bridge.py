from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from vibe.capx import CapXCompletionRequest, CapXImagePart, create_capx_app


class FakeCompletionAgent:
    def __init__(self) -> None:
        self.requests: list[CapXCompletionRequest] = []

    async def complete(self, request: CapXCompletionRequest, run_id: str) -> str:
        self.requests.append(request)
        return "```python\nprint('folded')\n```"


class FailingCompletionAgent:
    async def complete(self, request: CapXCompletionRequest, run_id: str) -> str:
        del request, run_id
        raise RuntimeError("secret-provider-detail")


@pytest.mark.asyncio
async def test_capx_bridge_accepts_multimodal_chat_completions(tmp_path: Path) -> None:
    agent = FakeCompletionAgent()
    app = create_capx_app(agent, trace_dir=tmp_path)
    image = base64.b64encode(b"fake-png").decode()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://robo.test"
    ) as client:
        response = await client.post(
            "/chat/completions",
            headers={"x-capx-trial-id": "trial-7"},
            json={
                "model": "robo-harness",
                "messages": [
                    {"role": "system", "content": "Return Python code."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Fold the cloth."},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{image}"},
                            },
                        ],
                    },
                ],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "```python\nprint('folded')\n```",
    }
    assert body["model"] == "robo-harness"
    assert response.headers["x-robo-run-id"]
    content = agent.requests[0].messages[1].content
    assert isinstance(content, list)
    assert isinstance(content[1], CapXImagePart)
    assert content[1].image_url is not None
    trace_path = next(tmp_path.glob("*.jsonl"))
    trace = trace_path.read_text()
    assert "print('folded')" not in trace
    assert '"output_chars": 29' in trace


@pytest.mark.asyncio
async def test_capx_bridge_rejects_invalid_image_data(tmp_path: Path) -> None:
    app = create_capx_app(FakeCompletionAgent(), trace_dir=tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://robo.test"
    ) as client:
        response = await client.post(
            "/chat/completions",
            json={
                "model": "robo-harness",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.com/a.png"},
                            }
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 400
    assert "data URL" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_capx_bridge_does_not_expose_internal_completion_errors(
    tmp_path: Path,
) -> None:
    app = create_capx_app(FailingCompletionAgent(), trace_dir=tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://robo.test"
    ) as client:
        response = await client.post(
            "/chat/completions", json={"model": "robo-harness", "messages": []}
        )

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "Robo completion failed"
    assert "secret-provider-detail" not in response.text
