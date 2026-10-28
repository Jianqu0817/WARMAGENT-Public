from __future__ import annotations

import numpy as np
import pytest

from myatri.memory.graph import MemoryGraph
from myatri.memory.vector_index import ExactVectorIndex
from warmagent.index_graph import FastMemoryGraph


def _index(view: str, rows: list[tuple[str, str, list[float]]]) -> ExactVectorIndex:
    return ExactVectorIndex(view, len(rows[0][2]), [
        {"ref": ref, "event_ref": anchor, "view": view, "space_id": view, "vector": vector}
        for ref, anchor, vector in rows
    ])


class _CountingMatrix(np.ndarray):
    def __new__(cls, values: np.ndarray):
        result = np.asarray(values).view(cls)
        result.exact_gemv_calls = 0
        return result

    def __array_finalize__(self, source):
        self.exact_gemv_calls = getattr(source, "exact_gemv_calls", 0)

    def __matmul__(self, other):
        if np.ndim(other) == 1:
            self.exact_gemv_calls += 1
        return super().__matmul__(other)


def _assert_edges_equal(actual: list[dict], expected: list[dict]) -> None:
    assert [edge["edge_id"] for edge in actual] == [edge["edge_id"] for edge in expected]
    for got, want in zip(actual, expected, strict=True):
        assert got["weight"] == pytest.approx(want["weight"], abs=1e-14)
        assert got["views"].keys() == want["views"].keys()
        for view in got["views"]:
            assert got["views"][view] == pytest.approx(want["views"][view], abs=1e-14)
        assert {key: value for key, value in got.items() if key not in {"weight", "views"}} == {
            key: value for key, value in want.items() if key not in {"weight", "views"}
        }


@pytest.mark.parametrize(("k", "mutual"), [(1, False), (2, False), (2, True)])
def test_cached_exact_scores_preserve_ties_anchors_and_edge_selection(monkeypatch, k: int, mutual: bool) -> None:
    event = _index("event", [
        ("a-query", "A", [1.0, 0.0, 0.0]),
        ("a-same-anchor", "A", [0.99, 0.1, 0.0]),
        ("z-tie", "Z", [0.8, 0.6, 0.0]),
        ("b-tie", "B", [0.8, 0.6, 0.0]),
        ("c", "C", [0.7, 0.0, 0.7]),
        ("d", "D", [0.4, 0.7, 0.5]),
        ("unavailable", "missing-node", [0.95, 0.2, 0.0]),
        ("e", "E", [-0.5, 0.0, 0.8]),
    ])
    feeling = _index("feeling", [
        ("a-feel", "A", [1.0, 0.0, 0.0]),
        ("b-feel", "B", [0.9, 0.1, 0.0]),
        ("c-feel", "C", [0.7, 0.0, 0.7]),
        ("d-feel", "D", [0.4, 0.7, 0.5]),
        ("z-feel", "Z", [0.8, 0.6, 0.0]),
        ("e-feel", "E", [-0.5, 0.0, 0.8]),
    ])
    indexes = {"event": event, "feeling": feeling}
    nodes = list("ABCDEZ")
    kwargs = {"k": k, "threshold": 0.08, "view_weights": {"event": 0.7, "feeling": 0.3}, "mutual": mutual}
    monkeypatch.setattr(FastMemoryGraph, "_QUERY_BLOCK", 2)
    monkeypatch.setattr(FastMemoryGraph, "_CANDIDATE_BLOCK", 2)

    expected = MemoryGraph(nodes)
    actual = FastMemoryGraph(nodes)
    expected.upsert_similarity_edges(indexes, **kwargs)
    actual.upsert_similarity_edges(indexes, **kwargs)

    _assert_edges_equal(actual.edges, expected.edges)


def test_boundary_exact_gemv_is_computed_once_per_query(monkeypatch) -> None:
    vectors = []
    for i in range(24):
        angle = i * 0.002
        vectors.append((f"ref-{i:02d}", f"node-{i:02d}", [1.0, angle, angle * angle]))
    index = _index("event", vectors)
    index.matrix = _CountingMatrix(index.matrix)
    nodes = [anchor for _, anchor, _ in vectors]
    monkeypatch.setattr(FastMemoryGraph, "_QUERY_BLOCK", 4)
    monkeypatch.setattr(FastMemoryGraph, "_CANDIDATE_BLOCK", 3)

    FastMemoryGraph(nodes).upsert_similarity_edges({"event": index}, k=2, threshold=0.0)

    assert index.matrix.exact_gemv_calls == len(vectors)
