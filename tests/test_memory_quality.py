"""Small synthetic regression cases for attribution, provenance and scoped recall."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from myatri.contracts import Event, Record, SourceSpan, digest
from myatri.memory.index import MemoryIndex
from myatri.storage import ArchiveStore
from warmagent.group_memory import GroupRecall
from warmagent.identity import IdentityRegistry
from warmagent.incremental_index import IncrementalMemoryIndex
from warmagent.claims import claim_projection
from warmagent.shared_runtime import MemoryVisibility


CASES = json.loads((Path(__file__).parent / "fixtures" / "memory_quality.json").read_text(encoding="utf-8"))


def _store(tmp_path):
    store = ArchiveStore(tmp_path / "archive", agent_id="warm", user_id="synthetic")
    scope = digest(CASES["scope"])[:32]
    records = []
    for item in CASES["messages"]:
        source = {"platform_id": CASES["scope"][0], "bot_id": CASES["scope"][1],
                  "group_id": CASES["scope"][2], "conversation_kind": item["conversation_kind"],
                  "sender_id": item["sender_id"], "sender_name": item["sender_name"],
                  "message_id": item["id"], "transport": "fixture", "text": item["text"]}
        raw = Record(record_id=item["id"], record_kind="raw_message", agent_id=store.agent_id,
                     user_id=store.user_id, author_kind="human", body_markdown=item["text"],
                     payload={"role": "user", "channel_source": source})
        records.append(raw)
    raw_by_id = {r.record_id: r for r in records}
    for rid, fact, author in (("fact-owner", "我把蓝色钥匙放在书架。", "owner-1"),
                              ("fact-member", "我把红色雨伞放在门口。", "member-2"),
                              ("fact-private", "我周末要去医院复诊。", "member-2")):
        source_id = {"fact-owner": "m-owner", "fact-member": "m-member", "fact-private": "m-private"}[rid]
        raw = raw_by_id[source_id]
        records.append(Record(record_id=rid, record_kind="event", agent_id=store.agent_id,
            user_id=store.user_id, author_kind="system", source_refs=[raw.ref], body_markdown=fact,
            payload=Event(participants=[author], fact_summary=fact, source_spans=[
                SourceSpan(message_ref=raw.ref, start=0, end=len(raw.body_markdown), quote=raw.body_markdown)],
                event_fingerprint=rid).model_dump()))
    old = Record(record_id="claim", record_kind="interpretation", agent_id=store.agent_id,
        user_id=store.user_id, author_kind="system", source_refs=[raw_by_id["m-owner"].ref],
        body_markdown=CASES["revisions"]["before"], payload={"status": "provisional"})
    records.append(old)
    store.commit(records, "base")
    return store, scope, raw_by_id, old


def _recall(store, scope, tmp_path, *, cache, private=False, sender="owner-1"):
    ids = IdentityRegistry(tmp_path / "identities.sqlite3", owner_id="owner-1", owner_name="阿星")
    recall = GroupRecall(store, None, ids, scope, scope_index_cache_enabled=cache)
    recall.shared_visibility = True
    recall.private_channel = private
    recall.current_sender = sender
    return recall


def test_identity_self_report_requires_matching_source_speaker(tmp_path):
    store, scope, raw, _ = _store(tmp_path)
    ids = IdentityRegistry(tmp_path / "ids.sqlite3", owner_id="owner-1", owner_name="阿星")
    envelope = raw["m-member"].payload["channel_source"]
    good = ids.validate_memory(scope, {"speaker_id": "member-2", "subject_id": "member-2",
        "attribution": "self_report", "evidence": [{"message_id": "m-member", "quote": "我把红色雨伞放在门口。"}]}, [envelope])
    mismatch = ids.validate_memory(scope, {"speaker_id": "member-2", "subject_id": "owner-1",
        "attribution": "self_report", "evidence": [{"message_id": "m-member", "quote": "我把红色雨伞放在门口。"}]}, [envelope])
    assert good["valid"] and good["normalized"]["confirmed"]
    assert mismatch == {"valid": False, "reason": "self_report_subject_mismatch", "normalized": None}


@pytest.mark.parametrize("cache", [False, True])
def test_group_recall_keeps_exact_visible_evidence_and_excludes_private_scope(tmp_path, cache):
    store, scope, raw, _ = _store(tmp_path)
    recall = _recall(store, scope, tmp_path, cache=cache)
    result = recall.recall("蓝色钥匙", budget=20000)
    refs = {record.ref for record in result.records}
    assert "fact-owner@1" in refs and raw["m-owner"].ref in refs
    assert raw["m-private"].ref not in refs and "fact-private@1" not in refs
    fact = next(record for record in result.records if record.ref == "fact-owner@1")
    assert set(fact.source_refs) <= refs
    assert any(path[0] == fact.ref and raw["m-owner"].ref in path for path in result.paths)
    assert MemoryVisibility(store, scope).allowed(fact)
    assert not MemoryVisibility(store, scope).allowed(store.get_record("fact-private"))


@pytest.mark.parametrize("cache", [False, True])
def test_private_view_does_not_reuse_group_scope_cache(tmp_path, cache):
    store, scope, raw, _ = _store(tmp_path)
    recall = _recall(store, scope, tmp_path, cache=cache)
    group_refs = {r.ref for r in recall.recall("医院复诊", budget=20000).records}
    recall.private_channel = True
    private_refs = {r.ref for r in recall.recall("医院复诊", budget=20000).records}
    assert raw["m-private"].ref not in group_refs
    assert raw["m-private"].ref in private_refs


@pytest.mark.parametrize("cache", [False, True])
def test_switch_scope_and_sender_does_not_reuse_private_or_other_group_records(tmp_path, cache):
    store, scope_a, _, _ = _store(tmp_path)
    scope_b = digest(CASES["other_scope"])[:32]
    other_source = {"platform_id": CASES["other_scope"][0], "bot_id": CASES["other_scope"][1],
        "group_id": CASES["other_scope"][2], "conversation_kind": "group", "sender_id": "member-b",
        "sender_name": "小乙", "message_id": "m-other", "transport": "fixture",
        "text": "另一群的私密蓝调线索。"}
    other_raw = Record(record_id="m-other", record_kind="raw_message", agent_id=store.agent_id,
        user_id=store.user_id, author_kind="human", body_markdown=other_source["text"],
        payload={"role": "user", "channel_source": other_source})
    other_fact = Record(record_id="fact-other", record_kind="event", agent_id=store.agent_id,
        user_id=store.user_id, author_kind="system", source_refs=[other_raw.ref],
        body_markdown="另一群的私密蓝调线索。", payload=Event(participants=["member-b"],
            fact_summary="另一群的私密蓝调线索。", source_spans=[SourceSpan(message_ref=other_raw.ref,
                start=0, end=len(other_raw.body_markdown), quote=other_raw.body_markdown)],
            event_fingerprint="fact-other").model_dump())
    store.commit([other_raw, other_fact], "other-scope")
    ids = IdentityRegistry(tmp_path / "identities.sqlite3", owner_id="owner-1", owner_name="阿星",
        owner_bindings=[{"platform_id": CASES["scope"][0], "bot_id": CASES["scope"][1], "sender_id": "owner-1"}])
    recall = GroupRecall(store, None, ids, scope_a, scope_index_cache_enabled=cache)
    recall.shared_visibility = True
    recall.current_sender = "owner-1"
    a_result = recall.recall("蓝色钥匙", budget=20000)
    assert "fact-owner@1" in {r.ref for r in a_result.records}
    # Prime A's scope/sender cache with its private source; private view allows it.
    recall.private_channel = True
    a_private = recall.recall("医院复诊", budget=20000)
    assert "fact-private@1" in {r.ref for r in a_private.records}
    recall.private_channel = False
    recall.scope = scope_b
    recall.current_sender = "member-b"
    current_source = {**other_source, "message_id": "m-current", "text": "当前群聊消息"}
    context = ids.context(scope_b, "member-b", source=current_source)
    assert context["owner"]["verified"] and context["current_member"]["member_id"] == "member-b"
    b_result = recall.recall("蓝调线索", budget=20000)
    refs = {r.ref for r in b_result.records}
    assert other_fact.ref in refs and other_raw.ref in refs
    assert "fact-private@1" not in refs and "m-private@1" not in refs
    assert "fact-owner@1" not in refs and "m-owner@1" not in refs
    assert set(other_fact.source_refs) <= refs


def test_claim_projection_uses_two_original_messages_and_historical_snapshot(tmp_path):
    store, _, _, _ = _store(tmp_path)
    source = CASES["scope"]
    messages = [
        ("tea-a", "我喜欢绿茶。", "2026-10-25T10:00:00+00:00"),
        ("tea-b", "更正：其实我喜欢红茶，不喜欢绿茶。", "2026-10-26T10:00:00+00:00"),
    ]
    raws, claims = [], []
    for message_id, text, observed in messages:
        raw = Record(record_id=message_id, record_kind="raw_message", agent_id=store.agent_id,
            user_id=store.user_id, author_kind="human", body_markdown=text,
            payload={"role": "user", "channel_source": {"platform_id": source[0], "bot_id": source[1],
                "group_id": source[2], "conversation_kind": "group", "sender_id": "owner-1",
                "message_id": message_id, "text": text, "transport": "fixture"}})
        raws.append(raw)
        correction = message_id == "tea-b"
        claim = Record(record_id=f"claim-{message_id}", record_kind="interpretation",
            agent_id=store.agent_id, user_id=store.user_id, author_kind="system", source_refs=[raw.ref],
            body_markdown="用户偏好红茶。" if correction else "用户偏好绿茶。",
            payload={"group_attribution": {"subject_id": "owner-1", "attribution": "self_report",
                    "source_message_ids": [message_id]},
                "claim_state": {"facet": "饮品偏好", "change": "correction" if correction else "assertion",
                    "supersedes_message_ids": ["tea-a"] if correction else [], "observed_at": [observed]}})
        claims.append(claim)
    store.commit([raws[0], claims[0]], "tea-a")
    old_snapshot = store.snapshot()
    old_view = claim_projection(store.list_records(snapshot=old_snapshot), "owner-1")
    store.commit([raws[1], claims[1]], "tea-b")
    current_view = claim_projection(store.list_records(), "owner-1")
    assert [row["ref"] for row in old_view["active"]] == [claims[0].ref]
    assert claims[1].ref not in {row["ref"] for row in old_view["history"]}
    assert [row["ref"] for row in current_view["active"]] == [claims[1].ref]
    history = {row["ref"]: row for row in current_view["history"]}
    assert history[claims[0].ref]["status"] == "superseded"
    assert history[claims[0].ref]["changed_by"] == claims[1].ref
    assert history[claims[1].ref]["source_message_ids"] == ["tea-b"]


def test_revision_and_historical_snapshot_preserve_old_and_current_evidence(tmp_path):
    store, scope, raw, old = _store(tmp_path)
    historical = store.snapshot()
    latest = Record(record_id="claim", revision=2, supersedes_revision=1,
        record_kind="interpretation", agent_id=store.agent_id, user_id=store.user_id,
        author_kind="system", source_refs=[raw["m-owner"].ref],
        body_markdown=CASES["revisions"]["after"], payload={"status": "corrected"})
    later = Record(record_id="later", record_kind="raw_message", agent_id=store.agent_id,
        user_id=store.user_id, author_kind="human", body_markdown=CASES["historical"]["later_only"], payload={"role": "user"})
    store.commit([latest, later], "correction-and-later", expected={"claim": 1})
    visibility = MemoryVisibility(store, scope)
    assert visibility.allowed(store.get_record("claim", 2))
    assert store.get_record("claim", 1, snapshot=historical).ref == old.ref
    recall = _recall(store, scope, tmp_path, cache=True)
    past = recall.recall("蓝色钥匙", snapshot=historical, budget=20000)
    current = recall.recall("蓝色钥匙", budget=20000)
    assert old.ref in {r.ref for r in past.records} and latest.ref not in {r.ref for r in past.records}
    assert latest.ref in {r.ref for r in current.records} and old.ref not in {r.ref for r in current.records}
    assert later.ref not in {r.ref for r in past.records}


@pytest.mark.asyncio
async def test_incremental_index_write_reopen_and_recall(tmp_path):
    store = ArchiveStore(tmp_path / "archive", agent_id="warm", user_id="synthetic")
    base_raw = Record(record_id="base-raw", record_kind="raw_message", agent_id="warm", user_id="synthetic",
        author_kind="human", body_markdown="星河喜欢秋天散步。", payload={"role": "user"})
    initial = Record(record_id="base", record_kind="event", agent_id="warm", user_id="synthetic",
        author_kind="system", source_refs=[base_raw.ref], body_markdown=base_raw.body_markdown,
        payload=Event(fact_summary=base_raw.body_markdown, source_spans=[SourceSpan(message_ref=base_raw.ref,
            start=0, end=len(base_raw.body_markdown), quote=base_raw.body_markdown)], event_fingerprint="base").model_dump())
    store.commit([base_raw, initial], "initial")
    base = MemoryIndex(store)
    await base.rebuild()
    index = IncrementalMemoryIndex.adapt(base, merge_delay_seconds=3600)
    try:
        raw = Record(record_id="new-raw", record_kind="raw_message", agent_id="warm", user_id="synthetic",
            author_kind="human", body_markdown="星河把备用钥匙放进蓝色铁盒。", payload={"role": "user"})
        event = Record(record_id="new-fact", record_kind="event", agent_id="warm", user_id="synthetic",
            author_kind="system", source_refs=[raw.ref], body_markdown="星河把备用钥匙放进蓝色铁盒。",
            payload=Event(fact_summary="星河把备用钥匙放进蓝色铁盒。", source_spans=[SourceSpan(
                message_ref=raw.ref, start=0, end=len(raw.body_markdown), quote=raw.body_markdown)],
                event_fingerprint="new-fact").model_dump())
        store.commit([raw, event], "new-fact")
        await index.update()
    finally:
        await index.close()
    reopened = IncrementalMemoryIndex.adapt(MemoryIndex(store), merge_delay_seconds=3600)
    try:
        await reopened.update()
        recall = _recall(store, digest(CASES["scope"])[:32], tmp_path, cache=True)
        recall.index = reopened
        recalled = recall.recall("备用钥匙 蓝色铁盒", budget=20000)
        refs = {r.ref for r in recalled.records}
        assert event.ref in refs and raw.ref in refs
        assert set(next(r for r in recalled.records if r.ref == event.ref).source_refs) <= refs
    finally:
        await reopened.close()
