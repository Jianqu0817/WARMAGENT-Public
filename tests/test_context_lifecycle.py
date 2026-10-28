import json

import pytest

from warmagent.context_lifecycle import (
    Audience,
    ContextItem,
    Lifecycle,
    exposed_receipt_ids,
    project_tool_messages,
)


def _tool(content, call_id="call-1"):
    return {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps(content, ensure_ascii=False)}


def test_first_exposure_keeps_long_receipt_verbatim_and_marks_pending():
    receipt = {"host_receipt_id": "call-1", "result": {"text": "x" * 1200}}
    messages = [{"role": "assistant", "tool_calls": [{"id": "call-1"}]}, _tool(receipt)]

    projected, stats = project_tool_messages(messages, set())

    assert projected == messages
    assert projected[1]["tool_call_id"] == "call-1"
    assert stats["folded_results"] == 0
    assert stats["pending_results"] == 1
    assert exposed_receipt_ids(messages) == {"call-1"}


def test_consumed_receipt_folds_without_mutating_original_or_breaking_pair():
    receipt = {"host_receipt_id": "call-1", "status": "completed",
               "result_preview": "Important output " + "x" * 1200,
               "receipt_ref": {"activity": "a1", "turn": "t2", "call_id": "call-1"},
               "full_receipt": {"file": "receipt.json"}}
    messages = [{"role": "assistant", "tool_calls": [{"id": "call-1"}]}, _tool(receipt)]
    original = json.loads(messages[1]["content"])

    projected, stats = project_tool_messages(messages, {"call-1"})
    folded = json.loads(projected[1]["content"])

    assert messages[1]["content"] == json.dumps(original, ensure_ascii=False)
    assert projected[1]["tool_call_id"] == "call-1"
    assert projected[0]["tool_calls"][0]["id"] == projected[1]["tool_call_id"]
    assert folded["host_receipt_id"] == "call-1"
    assert folded["status"] == "completed"
    assert folded["summary"].startswith("Important output")
    assert len(folded["summary"]) == 300
    assert folded["receipt_ref"] == {"activity": "a1", "turn": "t2", "call_id": "call-1"}
    assert folded["full_receipt"] == {"file": "receipt.json"}
    assert "不代表完整结果" in folded["note"]
    assert stats["folded_results"] == 1
    assert stats["projected_chars"] < stats["original_chars"]


@pytest.mark.parametrize("receipt", [
    {"host_receipt_id": "call-1", "error": "permission_denied", "result": "x" * 1200},
    {"host_receipt_id": "call-1", "result": {"status": "unknown", "text": "x" * 1200}},
    {"host_receipt_id": "call-1", "result": {"state": "unknown", "text": "x" * 1200}},
    {"host_receipt_id": "call-1", "result": {"isError": True, "text": "x" * 1200}},
    {"host_receipt_id": "call-1", "result": {"image_manifest": {"path": "x"}, "text": "x" * 1200}},
])
def test_errors_unknown_and_nested_images_are_left_verbatim(receipt):
    message = _tool(receipt)

    projected, stats = project_tool_messages([message], {"call-1"})

    assert projected[0]["content"] == message["content"]
    assert stats["folded_results"] == 0
    assert exposed_receipt_ids([message]) == set()


def test_small_and_unrecognized_messages_are_left_verbatim():
    messages = [
        _tool({"host_receipt_id": "call-1", "result": "short"}),
        {"role": "tool", "tool_call_id": "old", "content": "not-json"},
        {"role": "assistant", "content": json.dumps({"host_receipt_id": "call-2", "result": "x" * 1200})},
    ]

    projected, stats = project_tool_messages(messages, {"call-1", "old", "call-2"})

    assert projected == messages
    assert stats["folded_results"] == 0
    assert exposed_receipt_ids(messages) == set()


def test_context_item_validates_lifecycle_and_audience_contract():
    item = ContextItem("i", "source", "durable", "shared", "scope", None,
                       "summary", "payload", None, None, 1)
    assert item.lifecycle is Lifecycle.DURABLE
    assert item.audience is Audience.SHARED
    with pytest.raises(ValueError):
        ContextItem("i", "source", "unknown", "shared", "scope", None,
                    "summary", None, None, None, 1)
    with pytest.raises(ValueError):
        ContextItem("i", "source", "durable", "owner", "scope", None,
                    "summary", None, None, None, 1)
