from __future__ import annotations

import pytest

from vibe.capx import CapXCompletionRequest
from vibe.capx.local_bench import LocalCaPXBench


class RepairingAgent:
    def __init__(self) -> None:
        self.requests: list[CapXCompletionRequest] = []

    async def complete(self, request: CapXCompletionRequest, run_id: str) -> str:
        self.requests.append(request)
        if len(self.requests) == 1:
            return "```python\nfold_cloth(missing_name)\n```"
        return "REGENERATE\n```python\nfold_cloth('shirt')\nprint('done')\n```"


@pytest.mark.asyncio
async def test_local_bench_returns_stderr_for_a_second_turn() -> None:
    agent = RepairingAgent()
    result = await LocalCaPXBench(agent).run()

    assert result.success is True
    assert result.turns == 2
    assert result.folded_cloths == ["shirt"]
    assert "unsupported argument" in result.attempts[0].stderr
    second_prompt = agent.requests[1].messages[-1].content
    assert isinstance(second_prompt, str)
    assert "unsupported argument" in second_prompt
    assert "fold_cloth(missing_name)" in second_prompt
