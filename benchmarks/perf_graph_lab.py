from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import time
import types

import numpy as np
from threadpoolctl import threadpool_info, threadpool_limits


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from myatri.memory.vector_index import ExactVectorIndex  # noqa: E402
from warmagent.index_graph import FastMemoryGraph  # noqa: E402


BASELINE = ROOT / "benchmarks/baselines/index_graph_before.py"
SCORE_TOLERANCE = 1e-12
DEFAULT_CASES = ((400, 64), (800, 128), (1200, 256), (1200, 96), (2400, 96))


def _load_original_graph():
    source = BASELINE.read_text(encoding="utf-8")
    module = types.ModuleType("warmagent._perf_graph_original")
    exec(compile(source, str(BASELINE), "exec"), module.__dict__)
    return module.FastMemoryGraph


def _make_index(size: int, dimension: int, seed: int) -> tuple[ExactVectorIndex, list[str]]:
    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((size, dimension))
    nodes = [f"node-{i:05d}" for i in range(size)]
    rows = [
        {
            "ref": f"ref-{i:05d}",
            "event_ref": nodes[i],
            "view": "event",
            "space_id": "event",
            "vector": vectors[i].tolist(),
        }
        for i in range(size)
    ]
    return ExactVectorIndex("event", dimension, rows), nodes


def _run(graph_type, index: ExactVectorIndex, nodes: list[str]) -> tuple[float, list[dict]]:
    graph = graph_type(nodes)
    start = time.perf_counter()
    graph.upsert_similarity_edges(
        {"event": index}, k=5, threshold=0.08, view_weights={"event": 1.0}, mutual=False,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms, graph.edges


def _compare_edges(original: list[dict], cached: list[dict]) -> dict:
    original_by_id = {edge["edge_id"]: edge for edge in original}
    cached_by_id = {edge["edge_id"]: edge for edge in cached}
    same_ids = original_by_id.keys() == cached_by_id.keys()
    structure_equal = same_ids
    max_score_abs_diff = 0.0
    if same_ids:
        for edge_id, expected in original_by_id.items():
            actual = cached_by_id[edge_id]
            for key in ("from_ref", "to_ref", "type", "direction", "missing_view_policy", "status"):
                structure_equal &= actual.get(key) == expected.get(key)
            for view, score in expected["views"].items():
                max_score_abs_diff = max(max_score_abs_diff, abs(actual["views"][view] - score))
            max_score_abs_diff = max(max_score_abs_diff, abs(actual["weight"] - expected["weight"]))
    return {
        "edge_ids_equal": same_ids,
        "edge_structure_equal": structure_equal,
        "scores_within_tolerance": same_ids and max_score_abs_diff <= SCORE_TOLERANCE,
        "max_score_abs_diff": max_score_abs_diff,
        "score_abs_tolerance": SCORE_TOLERANCE,
        "original_edge_count": len(original),
        "cached_edge_count": len(cached),
    }


def _run_case(original_type, size: int, dimension: int, seed: int, repeats: int) -> dict:
    index, nodes = _make_index(size, dimension, seed)
    # Warm both implementations outside timed trials.
    _run(original_type, index, nodes)
    _run(FastMemoryGraph, index, nodes)

    original_ms: list[float] = []
    cached_ms: list[float] = []
    correctness: list[dict] = []
    for repeat in range(repeats):
        if repeat % 2 == 0:
            elapsed, original_edges = _run(original_type, index, nodes)
            original_ms.append(elapsed)
            elapsed, cached_edges = _run(FastMemoryGraph, index, nodes)
            cached_ms.append(elapsed)
        else:
            elapsed, cached_edges = _run(FastMemoryGraph, index, nodes)
            cached_ms.append(elapsed)
            elapsed, original_edges = _run(original_type, index, nodes)
            original_ms.append(elapsed)
        correctness.append(_compare_edges(original_edges, cached_edges))

    original_mean = float(np.mean(original_ms))
    cached_mean = float(np.mean(cached_ms))
    original_median = float(np.median(original_ms))
    cached_median = float(np.median(cached_ms))
    return {
        "nodes": size,
        "dimension": dimension,
        "seed": seed,
        "k": 5,
        "threshold": 0.08,
        "views": 1,
        "query_block": FastMemoryGraph._QUERY_BLOCK,
        "candidate_block": FastMemoryGraph._CANDIDATE_BLOCK,
        "repeats": repeats,
        "original_ms": original_ms,
        "cached_ms": cached_ms,
        "original_mean_ms": original_mean,
        "cached_mean_ms": cached_mean,
        "speedup": original_mean / cached_mean if cached_mean else None,
        "original_median_ms": original_median,
        "cached_median_ms": cached_median,
        "median_speedup": original_median / cached_median if cached_median else None,
        "correctness_each_repeat": correctness,
        "correctness_passed": all(
            item["edge_ids_equal"] and item["edge_structure_equal"] and item["scores_within_tolerance"]
            for item in correctness
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare the original and cached exact-score graph builders.")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "perf" / "graph-lab.json")
    args = parser.parse_args()
    if args.repeats <= 0:
        parser.error("--repeats must be positive")

    original_type = _load_original_graph()
    cases = []
    with threadpool_limits(limits=1):
        pools = threadpool_info()
        for size, dimension in DEFAULT_CASES:
            seed = 20261028
            cases.append(_run_case(original_type, size, dimension, seed, args.repeats))

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_source": str(BASELINE.relative_to(ROOT)),
        "comparison": "bundled baseline FastMemoryGraph vs working-tree FastMemoryGraph",
        "correctness": "same selected edge IDs and structure; weights and per-view scores within absolute tolerance",
        "score_abs_tolerance": SCORE_TOLERANCE,
        "blas_thread_limit": 1,
        "threadpools": pools,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "cases": cases,
        "research_note": (
            "Faiss recommends batched queries to use matrix-matrix multiplication. This implementation already "
            "scores query and candidate tiles in batches; the redundant exact fallback is kept as the original "
            "per-query GEMV and cached to preserve its floating-point values and tie behavior."
        ),
        "research_source": "https://github.com/facebookresearch/faiss/wiki/FAQ#why-are-searches-with-a-single-query-vector-slow",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for case in cases:
        print(
            f"n={case['nodes']} d={case['dimension']} "
            f"original={case['original_mean_ms']:.2f}ms cached={case['cached_mean_ms']:.2f}ms "
            f"speedup={case['speedup']:.3f}x correctness={case['correctness_passed']}"
        )
    print(f"report={args.output}")
    return 0 if all(case["correctness_passed"] for case in cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
