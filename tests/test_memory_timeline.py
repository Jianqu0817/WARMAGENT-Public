from datetime import datetime, timezone

from myatri.contracts import Event, Record, SourceSpan
from myatri.storage import ArchiveStore
from warmagent.memory_timeline import MemoryTimeline
from warmagent.retrieval_wave import offline_residual_structure_bonus


def _row(record_id, kind, body, when, scope, source_refs=None, revision=1, supersedes=None):
    payload = ({"occurred_at": when, "scope": scope} if kind != "event" else
        Event(occurred_at=when, participants=[], fact_summary=body,
            source_spans=[SourceSpan(message_ref=source_refs[0], start=0, end=1, quote="Q")],
            event_fingerprint=record_id, reality_scope="reported").model_dump())
    return Record(record_id=record_id, revision=revision, supersedes_revision=supersedes,
        record_kind=kind, agent_id="warmagent", user_id="timeline-test", author_kind="system",
        created_at=when, source_refs=source_refs or [], body_markdown=body,
        payload=({**payload, "role": "user"} if kind == "raw_message" else payload))


def test_timeline_filters_before_indexing_and_expands_exact_source_revision(tmp_path):
    store = ArchiveStore(tmp_path / "archive", agent_id="warmagent", user_id="timeline-test")
    when = "2026-10-20T10:00:00+00:00"
    raw = _row("raw_group", "raw_message", "Q群里原话：周五去图书馆。", when, "group")
    event = _row("event_group", "event", "群聊记忆摘要：周五去图书馆。", when, "group", [raw.ref])
    private_raw = _row("raw_private", "raw_message", "Q私聊原文：银杏。", when, "private")
    private = _row("event_private", "event", "私聊暗号：银杏。", when, "private", [private_raw.ref])
    store.commit([raw, event, private_raw, private], "initial")
    timeline = MemoryTimeline(store, lambda record: not record.record_id.endswith("private"))
    view = timeline.view("group", datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert view["source_watermark"] == store.snapshot()
    assert {item["record_ref"] for item in view["recent"]} == {event.ref}
    assert view["recent"][0]["summary_is_original_fact"] is False
    assert "2026-10" in view["months"]
    assert not any("private" in ref for ref in view["months"]["2026-10"]["record_refs"])
    expanded = timeline.expand("2026-10", "group")
    assert expanded["sources"][0]["ref"] == raw.ref
    assert expanded["sources"][0]["body_markdown"] == raw.body_markdown
    assert expanded["records"][0]["summary_is_original_fact"] is False


def test_timeline_cache_invalidates_on_new_manifest_and_does_not_rewrite_old_source(tmp_path):
    store = ArchiveStore(tmp_path / "archive", agent_id="warmagent", user_id="timeline-test")
    when = "2026-10-20T10:00:00+00:00"
    raw1 = _row("raw_one", "raw_message", "Q原始证据版本一。", when, "group")
    event1 = _row("event_one", "event", "事实摘要一。", when, "group", [raw1.ref])
    store.commit([raw1, event1], "first")
    timeline = MemoryTimeline(store, lambda record: True)
    first = timeline.view("group", "2026-10-25T00:00:00Z")
    assert timeline.view("group", "2026-10-25T00:00:00Z") is first
    raw2 = _row("raw_two", "raw_message", "Q新增原始证据版本二。", "2026-10-21T10:00:00+00:00", "group")
    event2 = _row("event_two", "event", "新增事实摘要二。", "2026-10-21T10:00:00+00:00", "group", [raw2.ref])
    store.commit([raw2, event2], "second")
    second = timeline.view("group", "2026-10-25T00:00:00Z")
    assert second is not first
    assert second["source_watermark"] != first["source_watermark"]
    expanded = timeline.expand("2026-10", "group")
    assert raw1.ref in {source["ref"] for source in expanded["sources"]}
    assert raw2.ref in {source["ref"] for source in expanded["sources"]}
    assert next(source for source in expanded["sources"] if source["ref"] == raw1.ref)["body_markdown"] == "Q原始证据版本一。"


def test_offline_residual_structure_bonus_is_bounded_ranking_signal():
    scores = offline_residual_structure_bonus({"a": 1.0, "b": 0.5}, {"a": 0.0, "b": 1.0},
        {"a": 1.0, "b": 0.0})
    assert max(scores.values()) - min(scores.values()) <= 1.0
    assert scores["a"] > scores["b"]

def test_timeline_transport_page_stays_under_byte_budget_and_preserves_lookup_refs(tmp_path):
    import json
    store = ArchiveStore(tmp_path / "archive", agent_id="warmagent", user_id="timeline-test")
    when = "2026-10-24T10:00:00+00:00"
    raw_text = "Q" + "long-original-" * 900
    raw = _row("raw_large", "raw_message", raw_text, when, "group")
    event = _row("event_large", "event", "Derived memory summary", when, "group", [raw.ref])
    store.commit([raw, event], "large-source")
    timeline = MemoryTimeline(store, lambda record: True)
    view = timeline.view("group", "2026-10-25T00:00:00Z", budget_chars=5500)
    assert len(json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 5500
    expanded = timeline.expand("2026-10", "group", budget_chars=5500)
    assert len(json.dumps(expanded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 5500
    source = expanded["sources"][0]
    assert source["truncated"] is True
    assert source["read_record_ref"] == raw.ref
    assert expanded["source_watermark"] == store.snapshot()
