import copy
import json

import pytest

from agent_harness.coding.context import request_messages
from agent_harness.coding.types import CodingError


def tool_call(key):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": key,
                "type": "function",
                "function": {"name": "program_events", "arguments": "{}"},
            }
        ],
    }


def frame(key, sampled_at):
    return {
        "type": "image_ref",
        "id": key * 64,
        "mime": "image/png",
        "metadata": {"sampled_at": sampled_at, "source": "recorded-camera"},
    }


def history(old_content):
    return [
        {"role": "user", "content": "Inspect evidence. Do not start another job."},
        tool_call("old"),
        {"role": "tool", "tool_call_id": "old", "content": old_content},
        {"role": "assistant", "content": "Inspect current evidence next."},
        tool_call("current"),
        {
            "role": "tool",
            "tool_call_id": "current",
            "content": [
                {"type": "text", "text": "current details " * 250 + " uncertain action"},
                frame("b", 2),
            ],
        },
        {"role": "assistant", "content": "Release remains unverified."},
    ]


@pytest.mark.parametrize("multimodal", [False, True])
def test_old_tool_text_keeps_head_tail_images_and_recent_exchange(multimodal):
    old_text = "ORIGINAL JOB ID\n" + "old event data\n" * 1500 + "\nFINAL UNKNOWN"
    old_content = (
        [{"type": "text", "text": old_text}, frame("a", 1)]
        if multimodal
        else old_text
    )
    messages = history(old_content)
    original = copy.deepcopy(messages)

    compacted, changed = request_messages("System", messages, 12_000)

    assert changed and len(json.dumps(compacted)) < 12_000
    old = next(m for m in compacted if m.get("tool_call_id") == "old")
    text = old["content"][0]["text"] if multimodal else old["content"]
    assert len(text) < 2200
    assert text.startswith("ORIGINAL JOB ID\n")
    assert text.endswith("\nFINAL UNKNOWN")
    assert "shortened" in text
    if multimodal:
        assert old["content"][1:] == old_content[1:]
    assert compacted[-4:] == original[-4:]
    assert original[0] in compacted
    assert messages == original  # Never rewrite the durable transcript.
    calls = {c["id"] for m in compacted for c in m.get("tool_calls", [])}
    results = {m["tool_call_id"] for m in compacted if m["role"] == "tool"}
    assert calls == results


def test_old_multipart_output_preserves_part_order_and_image_metadata():
    content = [
        {"type": "text", "text": "FIRST\n" + "x" * 10_000 + "\nFIRST END"},
        frame("a", 1),
        {"type": "text", "text": "SECOND\n" + "y" * 10_000 + "\nSECOND END"},
        frame("c", 1.5),
    ]
    messages = history(content)
    compacted, changed = request_messages("System", messages, 12_000)
    assert changed
    parts = next(m["content"] for m in compacted if m.get("tool_call_id") == "old")
    assert [p["type"] for p in parts] == [p["type"] for p in content]
    assert parts[1] == content[1] and parts[3] == content[3]
    for index in (0, 2):
        assert len(parts[index]["text"]) < 2200
        assert parts[index]["text"].endswith(content[index]["text"][-20:])
    assert messages[2]["content"] == content


def test_under_budget_history_remains_exact():
    messages = history([{"type": "text", "text": "short output"}, frame("a", 1)])
    compacted, changed = request_messages("System", messages, 12_000)
    assert not changed
    assert compacted[1:] == messages


@pytest.mark.parametrize("multimodal", [False, True])
def test_oversized_active_exchange_is_not_silently_shortened(multimodal):
    content = "ACTIVE EVIDENCE " * 1000
    if multimodal:
        content = [{"type": "text", "text": content}, frame("a", 1)]
    messages = [
        {"role": "user", "content": "Do not release without evidence."},
        tool_call("active"),
        {"role": "tool", "tool_call_id": "active", "content": content},
    ]
    original = copy.deepcopy(messages)
    with pytest.raises(CodingError, match="current request/tool exchange exceeds"):
        request_messages("System", messages, 4000)
    assert messages == original
