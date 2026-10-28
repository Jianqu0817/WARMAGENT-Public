"""Pure projections for context item contracts and tool receipt lifecycles."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class Lifecycle(str, Enum):
    EPHEMERAL = "ephemeral"
    DURABLE = "durable"
    STATUS = "status"
    DASHBOARD = "dashboard"


class Audience(str, Enum):
    AI = "ai"
    HUMAN = "human"
    SHARED = "shared"


# Host producer contracts, independent of what a retrieved text claims to be.
SOURCE_POLICIES = {
    'rules': ('durable', 'ai'), 'dialogue': ('durable', 'shared'),
    'recall': ('durable', 'ai'), 'summary': ('durable', 'ai'),
    'imported_history': ('durable', 'ai'), 'state': ('status', 'ai'),
    'group_policy_and_summary': ('durable', 'ai'), 'timeline_and_current': ('durable', 'shared'),
    'environment': ('dashboard', 'ai'), 'tool_progress': ('ephemeral', 'ai'),
    'tool_result': ('durable', 'ai'), 'topic': ('status', 'ai'),
}


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    source_ref: str
    lifecycle: Lifecycle | str
    audience: Audience | str
    scope: str
    task_id: str | None
    summary: str
    payload_ref: str | None
    expires_at: Any | None
    observed_at: Any | None
    revision: int

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "lifecycle", Lifecycle(self.lifecycle))
        except ValueError as exc:
            raise ValueError(f"unsupported lifecycle: {self.lifecycle!r}") from exc
        try:
            object.__setattr__(self, "audience", Audience(self.audience))
        except ValueError as exc:
            raise ValueError(f"unsupported audience: {self.audience!r}") from exc


def _receipt(message: Mapping[str, Any]) -> tuple[dict[str, Any], str] | None:
    if message.get("role") != "tool" or not isinstance(message.get("content"), str):
        return None
    try:
        value = json.loads(message["content"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("host_receipt_id"), str):
        return None
    call_id = message.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    return value, call_id


def _contains_image_manifest(value: Any) -> bool:
    if isinstance(value, dict):
        return "image_manifest" in value or any(_contains_image_manifest(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_image_manifest(v) for v in value)
    return False


def _is_error_or_unknown(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "error" and child:
                return True
            if key == "isError" and child is True:
                return True
            if key == "status" and isinstance(child, str) and child.lower() in {
                "error", "failed", "unknown", "outcome_unknown",
            }:
                return True
            if key == "state" and isinstance(child, str) and child.lower() in {
                "error", "failed", "unknown", "outcome_unknown",
            }:
                return True
            if _is_error_or_unknown(child):
                return True
    elif isinstance(value, list):
        return any(_is_error_or_unknown(child) for child in value)
    elif isinstance(value, str) and value.lower() == "outcome_unknown":
        return True
    return False


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _eligible(messages: list[dict[str, Any]], index: int) -> tuple[dict[str, Any], str] | None:
    pair = _receipt(messages[index])
    if pair is None:
        return None
    receipt, call_id = pair
    content = messages[index]["content"]
    if len(content) <= 900 or _is_error_or_unknown(receipt) or _contains_image_manifest(receipt):
        return None
    return receipt, call_id


def exposed_receipt_ids(messages: list[dict[str, Any]]) -> set[str]:
    """Return call IDs for large, successful, text-only Harness receipts."""
    result: set[str] = set()
    for index in range(len(messages)):
        eligible = _eligible(messages, index)
        if eligible:
            result.add(eligible[1])
    return result


def project_tool_messages(
    messages: list[dict[str, Any]], consumed_call_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Copy messages and fold consumed long Harness receipts into summaries."""
    projected = copy.deepcopy(messages)
    folded = 0
    pending = 0
    observed_image_indexes = [index for index, message in enumerate(messages)
        if (pair := _receipt(message)) and pair[1] in consumed_call_ids and _contains_image_manifest(pair[0])]
    latest_observed_image = max(observed_image_indexes, default=-1)
    for index in range(len(projected)):
        eligible = _eligible(projected, index)
        if eligible is None and index < latest_observed_image:
            image_receipt = _receipt(projected[index])
            if (image_receipt and image_receipt[1] in consumed_call_ids
                    and _contains_image_manifest(image_receipt[0]) and not _is_error_or_unknown(image_receipt[0])):
                eligible = image_receipt
        if eligible is None:
            continue
        receipt, call_id = eligible
        if call_id not in consumed_call_ids:
            pending += 1
            continue
        summary_source = receipt.get("result_preview", projected[index]["content"])
        if not isinstance(summary_source, str):
            summary_source = projected[index]["content"]
        folded_receipt: dict[str, Any] = {
            "host_receipt_id": receipt["host_receipt_id"],
        }
        if "status" in receipt:
            folded_receipt["status"] = receipt["status"]
        folded_receipt["summary"] = summary_source[:300]
        folded_receipt["receipt_ref"] = receipt.get("receipt_ref") or {"call_id": call_id}
        if "full_receipt" in receipt:
            folded_receipt["full_receipt"] = receipt["full_receipt"]
        if receipt.get('image_manifest'):
            folded_receipt['image_manifest_ref'] = receipt['image_manifest']
        folded_receipt["note"] = (
            "摘要仅展示回执前段，不代表完整结果；需要原文时调用 harness_read_receipt "
            "读取已有回执，不要重新执行原操作。"
        )
        projected[index]["content"] = _compact(folded_receipt)
        folded += 1

    original_chars = sum(len(message.get("content", "")) for message in messages
                         if isinstance(message, dict) and isinstance(message.get("content"), str))
    projected_chars = sum(len(message.get("content", "")) for message in projected
                          if isinstance(message, dict) and isinstance(message.get("content"), str))
    return projected, {
        "original_chars": original_chars,
        "projected_chars": projected_chars,
        "folded_results": folded,
        "pending_results": pending,
    }
