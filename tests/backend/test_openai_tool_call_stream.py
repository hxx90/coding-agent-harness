from __future__ import annotations

import json

import pytest

from vibe.core.agent_loop._generation import (
    IncompleteGenerationError,
    validate_generation,
)
from vibe.core.config import ProviderConfig
from vibe.core.llm.backend.generic import OpenAIAdapter


def delta(index, *, call_id=None, name=None, arguments=""):
    call = {"index": index, "function": {"arguments": arguments}}
    if call_id is not None:
        call["id"] = call_id
    if name is not None:
        call["function"]["name"] = name
    return {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": None}]}


async def collect(events, *, reason="tool_calls"):
    async def source():
        for event in events:
            yield event
        yield {"choices": [{"delta": {}, "finish_reason": reason}]}

    result = None
    provider = ProviderConfig(name="test", api_base="http://localhost/v1")
    async for parsed in OpenAIAdapter().parse_stream(source(), provider):
        result = parsed.chunk if result is None else result + parsed.chunk
    assert result is not None
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("second_index", [0, 1])
@pytest.mark.parametrize("second_name", ["robo_devices", "robo_observe"])
async def test_sequential_distinct_calls_keep_their_arguments(
    second_index, second_name
):
    # Captured gateway shape: each tool starts with a distinct ID but index=0.
    result = await collect([
        delta(0, call_id="connect-call", name="robo_devices"),
        delta(0, arguments='{"action":'),
        delta(0, arguments='"connect"}'),
        delta(second_index, call_id="acquire-call", name=second_name),
        delta(second_index, arguments='{"action":"acquire"}'),
    ])
    validate_generation(result, require_reason=True)
    calls = result.message.tool_calls or []
    assert len(calls) == 2
    assert [c.id for c in calls] == ["connect-call", "acquire-call"]
    assert [json.loads(c.function.arguments or "") for c in calls] == [
        {"action": "connect"},
        {"action": "acquire"},
    ]
    assert len({c.index for c in calls}) == 2


@pytest.mark.asyncio
async def test_interleaved_distinct_wire_indices_still_work():
    result = await collect([
        delta(0, call_id="a", name="one", arguments='{"a":'),
        delta(1, call_id="b", name="two", arguments='{"b":'),
        delta(0, arguments="1}"),
        delta(1, arguments="2}"),
    ])
    assert [
        json.loads(c.function.arguments or "")
        for c in (result.message.tool_calls or [])
    ] == [{"a": 1}, {"b": 2}]


@pytest.mark.asyncio
async def test_reused_index_cannot_steal_an_incomplete_call():
    with pytest.raises(ValueError, match="incomplete"):
        await collect([
            delta(0, call_id="a", name="one", arguments='{"a":'),
            delta(0, call_id="b", name="one", arguments="1}"),
        ])


@pytest.mark.asyncio
async def test_normalized_indices_do_not_collide_with_later_wire_indices():
    result = await collect([
        delta(0, call_id="a", name="one", arguments='{"a":1}'),
        delta(0, call_id="b", name="one", arguments='{"b":2}'),
        delta(1, call_id="c", name="one", arguments='{"c":'),
        delta(1, arguments="3}"),
    ])
    calls = result.message.tool_calls or []
    assert len({c.index for c in calls}) == 3
    assert [json.loads(c.function.arguments or "") for c in calls] == [
        {"a": 1},
        {"b": 2},
        {"c": 3},
    ]


@pytest.mark.asyncio
async def test_valid_prefix_with_length_finish_is_still_rejected():
    result = await collect(
        [delta(0, call_id="a", name="one", arguments="{}")], reason="length"
    )
    with pytest.raises(IncompleteGenerationError):
        validate_generation(result, require_reason=True)


@pytest.mark.asyncio
async def test_concatenated_json_within_one_call_is_not_repaired():
    result = await collect([delta(0, call_id="a", name="one", arguments="{}{}")])
    with pytest.raises(IncompleteGenerationError):
        validate_generation(result, require_reason=True)


@pytest.mark.asyncio
async def test_legacy_null_id_continuation_keeps_original_identity():
    result = await collect([
        delta(0, call_id="a", name="one", arguments='{"a":'),
        delta(0, call_id="null", name="", arguments="1}"),
    ])
    calls = result.message.tool_calls or []
    assert calls[0].id == "a"
    assert json.loads(calls[0].function.arguments or "") == {"a": 1}
