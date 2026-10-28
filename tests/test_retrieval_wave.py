"""Offline bounded-wave contract and small ablation over the runtime fixture."""
import time

from myatri.contracts import Event, Record, SourceSpan
from myatri.storage import ArchiveStore
from warmagent.retrieval_wave import ScopedWaveCandidates, hub_suppress_wave
from test_runtime_retrieval_baseline import _scope_recall


def test_wave_is_bounded_nonbacktracking_and_preserves_direct_facts():
    a, b, c, outside = "a", "b", "c", "private"
    result = hub_suppress_wave({a: 1.0, b: 0.0, c: 0.0}, [
        {"from_ref": a, "to_ref": b, "type": "supports"},
        {"from_ref": b, "to_ref": c, "type": "related"},
        {"from_ref": c, "to_ref": outside, "type": "related"},
        {"from_ref": a, "to_ref": c, "type": "contradicts"},
    ], snapshot="s1", scope="group", direct_refs={a}, hops=2)
    assert result.order[0] == a
    assert all(edge["to_ref"] != outside for edge in result.edge_flow)
    assert all(edge["type"] != "contradicts" for edge in result.edge_flow)
    assert not any(edge["from_ref"] == b and edge["to_ref"] == a and edge["step"] == 2
                   for edge in result.edge_flow)
    assert result.cache_key[:2] == ("s1", "group")


def test_wave_ablation_on_fixture_reports_recall_citations_latency_and_scope(tmp_path):
    measurements = []
    for index, (scope, query, expected) in enumerate([
        ("group", "群里约定周五下午几点开读书会？", "group_friday_meeting"),
        ("group", "后来明确纠正后，他更喜欢红茶还是绿茶？", "group_tea_correction"),
        ("private", "秋日暗号是什么？", "private_secret_word"),
    ]):
        recall, refs = _scope_recall(tmp_path / str(index), scope)
        started = time.perf_counter()
        bundle = recall.recall(query)
        base_ms = (time.perf_counter() - started) * 1000
        found = [record.ref for record in bundle.records]
        expected_ref = refs[expected]
        edges = [{"from_ref": path[0], "to_ref": path[-1], "type": "evidence"}
                 for path in bundle.paths if len(path) > 1]
        base = {ref: 1 / (rank + 1) for rank, ref in enumerate(found)}
        started = time.perf_counter()
        wave = hub_suppress_wave(base, edges, snapshot=bundle.snapshot or "latest", scope=scope,
                                 direct_refs={expected_ref} if expected_ref in base else set())
        wave_ms = (time.perf_counter() - started) * 1000
        assert expected_ref in found[:3]
        assert expected_ref in wave.order[:3]
        expected_record = next(record for record in bundle.records if record.ref == expected_ref)
        relevant_refs = {expected_ref, *expected_record.source_refs}
        assert any(ref in found for ref in expected_record.source_refs)
        if scope == "group":
            assert all(not ref.startswith("private_") for ref in wave.order)
        measurements.append({"scope": scope, "base_recall_at_3": int(expected_ref in found[:3]),
            "wave_recall_at_3": int(expected_ref in wave.order[:3]),
            "base_top3_unrelated": sum(ref not in relevant_refs for ref in found[:3]),
            "wave_top3_unrelated": sum(ref not in relevant_refs for ref in wave.order[:3]),
            "citation_present": int(any(ref in found for ref in expected_record.source_refs)),
            "base_ms": round(base_ms, 3), "wave_ms": round(wave_ms, 3)})
    # Kept as executable local evidence rather than a latency claim or threshold.
    assert len(measurements) == 3
    print("retrieval-wave ablation (synthetic; timing is local only):", measurements)
def _event(store_id, text, source, when):
    raw = Record(record_id=f"raw_{store_id}", record_kind="raw_message", agent_id="warmagent",
        user_id="wave-fixture", author_kind="human", created_at=when, body_markdown=source,
        payload={"role": "user", "scope": "group" if not store_id.startswith("private") else "private"})
    event = Record(record_id=store_id, record_kind="event", agent_id="warmagent", user_id="wave-fixture",
        author_kind="system", created_at=when, source_refs=[raw.ref], body_markdown=text,
        payload=Event(occurred_at=when, participants=[], fact_summary=text,
            source_spans=[SourceSpan(message_ref=raw.ref, start=0, end=1, quote=source[0])],
            event_fingerprint=store_id, reality_scope="reported").model_dump())
    return raw, event


def _edge(edge_id, a, b, kind="supports"):
    return Record(record_id=edge_id, record_kind="edge", agent_id="warmagent", user_id="wave-fixture",
        author_kind="system", source_refs=[a, b], body_markdown=f"{kind} link",
        payload={"edge_id": edge_id, "from_ref": a, "to_ref": b, "type": kind,
                 "evidence_refs": [a, b]})


def test_scoped_wave_expands_remote_event_but_ignores_private_bridge(tmp_path):
    store = ArchiveStore(tmp_path / "archive", agent_id="warmagent", user_id="wave-fixture")
    at = "2026-10-20T00:00:00+00:00"
    raw1, e1 = _event("event_1", "chain one", "A source", at)
    raw2, e2 = _event("event_2", "chain two", "B source", at)
    raw3, e3 = _event("event_3", "remote chain target", "C source", at)
    raw4, e4 = _event("event_remote", "private bridge only", "D source", at)
    rawp, ep = _event("private_bridge", "private bridge node", "P source", at)
    rows = [raw1, e1, raw2, e2, raw3, e3, raw4, e4, rawp, ep,
        _edge("edge_12", e1.ref, e2.ref), _edge("edge_23", e2.ref, e3.ref),
        _edge("edge_private_a", e2.ref, ep.ref), _edge("edge_private_b", ep.ref, e4.ref),
        _edge("edge_contradicts", e1.ref, e3.ref, "contradicts")]
    store.commit(rows, "wave-fixture")
    def allowed(record):
        return not record.record_id.startswith("private_") and not record.record_id.startswith("edge_private")
    expander = ScopedWaveCandidates(store, allowed)
    baseline_candidate_refs = {e1.ref}
    result = expander.expand({e1.ref: 1.0}, "group", store.snapshot(), max_candidates=10, hops=2)
    refs = {record.ref for record in result.candidates}
    assert e3.ref not in baseline_candidate_refs
    assert e3.ref in refs
    assert e4.ref not in refs and ep.ref not in refs
    assert result.paths[e3.ref] == (e1.ref, e2.ref, e3.ref)
    assert "contradicts" not in {edge["type"] for edge in result.edge_flow}
    print({"candidate_recall": {"base": int(e3.ref in baseline_candidate_refs),
        "expanded": int(e3.ref in refs)}, "expanded_order": [record.record_id for record in result.candidates],
        "private_bridge_leak": int(e4.ref in refs or ep.ref in refs),
        "target_rank": [record.ref for record in result.candidates].index(e3.ref) + 1})
