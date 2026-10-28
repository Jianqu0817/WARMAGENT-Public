from __future__ import annotations

import asyncio

import numpy as np
import pytest

from myatri.contracts import Event, Record, SourceSpan
from myatri.memory.graph import MemoryGraph
from myatri.memory.index import MemoryIndex
from myatri.memory.vector_index import ExactVectorIndex
from myatri.storage import ArchiveStore
from warmagent.index_builder import GraphMemoryIndex
from warmagent.index_graph import FastMemoryGraph


def _index(view: str, vectors: list[tuple[str, str, list[float]]]) -> ExactVectorIndex:
    rows = [
        {"ref": ref, "event_ref": anchor, "view": view, "space_id": view, "vector": vector}
        for ref, anchor, vector in vectors
    ]
    return ExactVectorIndex(view, len(vectors[0][2]), rows)


def _indexes() -> dict[str, ExactVectorIndex]:
    return {
        "event": _index("event", [
            ("a-1", "A", [1, 0]),
            ("a-2", "A", [0.99, 0.1]),
            ("b", "B", [0.8, 0.6]),
            ("f", "F", [0.8, 0.6]),  # exact tie: ref order breaks it
            ("c", "C", [0.8, -0.6]),
            ("d", "D", [0, 1]),
            ("e", "E", [-1, 0]),
            ("zero", "Z", [0, 0]),
        ]),
        "feeling": _index("feeling", [
            ("a-feel", "A", [1, 0]),
            ("b-feel", "B", [0.9, 0.1]),
            ("c-feel", "C", [0.7, -0.7]),
            ("d-feel", "D", [0, 1]),
            ("e-feel", "E", [-1, 0]),
            ("f-feel", "F", [0.9, 0.1]),
        ]),
        "ignored": _index("ignored", [("a-ignored", "A", [1, 0]), ("b-ignored", "B", [1, 0])]),
    }


def _assert_edges_equal(actual: list[dict], expected: list[dict]) -> None:
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        assert got.keys() == want.keys()
        for key in got:
            if key == "weight":
                assert got[key] == pytest.approx(want[key])
            elif key == "views":
                assert got[key].keys() == want[key].keys()
                for view in got[key]:
                    assert got[key][view] == pytest.approx(want[key][view])
            else:
                assert got[key] == want[key]


@pytest.mark.parametrize(("k", "mutual"), [(1, False), (2, False), (2, True)])
def test_chunked_similarity_edges_match_frozen_graph(k: int, mutual: bool) -> None:
    nodes = list("ABCDEFZ")
    semantic = {
        "edge_id": "semantic-1", "from_ref": "A", "to_ref": "Z", "type": "supports",
        "direction": "directed", "weight": 1.0,
    }
    stale = {"edge_id": "old-sim", "from_ref": "A", "to_ref": "B", "type": "similar"}
    expected = MemoryGraph(nodes, [semantic, stale])
    actual = FastMemoryGraph(nodes, [semantic, stale])
    kwargs = {"k": k, "threshold": 0.08, "view_weights": {"event": 0.7, "feeling": 0.3}, "mutual": mutual}

    expected.upsert_similarity_edges(_indexes(), **kwargs)
    actual.upsert_similarity_edges(_indexes(), **kwargs)

    _assert_edges_equal(actual.edges, expected.edges)
    assert semantic in actual.edges


def test_graph_memory_index_rebuild_matches_frozen_index(tmp_path) -> None:
    user_id = "index-graph-diff"
    records = []
    facts = [
        "我们在秋天去湖边看星空和流星。",
        "湖边夜晚可以观察星星与流星雨。",
        "午餐喜欢吃番茄鸡蛋面。",
        "周末计划整理书架和旧书。",
        "秋夜的星空很适合拍照。",
    ]
    for i, fact in enumerate(facts):
        raw = Record(record_id=f"raw-{i}", record_kind="raw_message", agent_id="warmagent",
                     user_id=user_id, author_kind="human", created_at=f"2026-10-{i + 1:02d}T10:00:00+00:00",
                     body_markdown=fact, payload={"role": "user", "sent_at": f"2026-10-{i + 1:02d}T10:00:00+00:00"})
        event = Record(record_id=f"event-{i}", record_kind="event", agent_id="warmagent",
                       user_id=user_id, author_kind="system", created_at=f"2026-10-{i + 1:02d}T10:00:00+00:00",
                       source_refs=[raw.ref], body_markdown=fact,
                       payload=Event(occurred_at=f"2026-10-{i + 1:02d}T10:00:00+00:00", participants=[],
                                     fact_summary=fact,
                                     source_spans=[SourceSpan(message_ref=raw.ref, start=0, end=len(fact), quote=fact)],
                                     event_fingerprint=f"event-{i}", reality_scope="reported").model_dump())
        records.extend((raw, event))

    results = []
    for name, index_type in (("frozen", MemoryIndex), ("fast", GraphMemoryIndex)):
        store = ArchiveStore(tmp_path / name, agent_id="warmagent", user_id=user_id)
        store.commit(records, idempotency_key="graph-index-differential")
        index = index_type(store)
        asyncio.run(index.rebuild())
        graph = index.graph.state()
        graph.pop("generation")
        clusters = dict(index.clusters)
        clusters.pop("graph_generation")
        clusters.pop("cluster_run_id")
        results.append((graph, clusters, index.themes, index.query("秋天湖边的星空")))

    frozen, fast = results
    assert fast[0] == frozen[0]
    assert fast[1] == frozen[1]
    assert fast[2] == frozen[2]
    assert [item["ref"] for item in fast[3]] == [item["ref"] for item in frozen[3]]
    assert [item["score"] for item in fast[3]] == pytest.approx([item["score"] for item in frozen[3]])


def test_chunked_top_k_matches_frozen_graph_across_small_blocks(monkeypatch) -> None:
    rng = np.random.default_rng(2741)
    bases = rng.normal(size=(6, 32))
    vectors = []
    for group, base in enumerate(bases):
        for offset in range(4):
            vector = base.copy()
            vector[(group + offset) % len(vector)] += (offset + 1) * 2e-15
            vectors.append((f"r{(len(vectors) * 7) % 24:02d}", f"N{len(vectors):02d}", vector.tolist()))
    index = _index("event", vectors)
    indexes = {"event": index}
    nodes = [anchor for _, anchor, _ in vectors]
    expected = MemoryGraph(nodes)
    actual = FastMemoryGraph(nodes)
    monkeypatch.setattr(FastMemoryGraph, "_QUERY_BLOCK", 2)
    monkeypatch.setattr(FastMemoryGraph, "_CANDIDATE_BLOCK", 3)

    expected.upsert_similarity_edges(indexes, k=2, threshold=0.08)
    actual.upsert_similarity_edges(indexes, k=2, threshold=0.08)

    actual_by_id = {edge["edge_id"]: edge for edge in actual.edges}
    expected_by_id = {edge["edge_id"]: edge for edge in expected.edges}
    assert actual_by_id.keys() == expected_by_id.keys()
    _assert_edges_equal(
        [actual_by_id[key] for key in sorted(actual_by_id)],
        [expected_by_id[key] for key in sorted(expected_by_id)],
    )
