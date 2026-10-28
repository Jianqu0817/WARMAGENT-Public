"""Chunked exact similarity graph adapter for the frozen MemoryGraph core."""
from __future__ import annotations

import numpy as np

from myatri.contracts import digest
from myatri.memory.graph import MemoryGraph
from myatri.memory.vector_index import ExactVectorIndex


class FastMemoryGraph(MemoryGraph):
    """Build the same similarity edges using bounded matrix-multiply tiles."""

    _QUERY_BLOCK = 64
    _CANDIDATE_BLOCK = 1024

    def upsert_similarity_edges(
        self,
        indexes: dict[str, ExactVectorIndex],
        *,
        k: int = 5,
        threshold: float = 0.08,
        view_weights: dict[str, float] | None = None,
        mutual: bool = False,
    ) -> None:
        weights = view_weights or {"event": 0.7, "feeling": 0.3}
        directed: dict[tuple[str, str], dict[str, float]] = {}
        nodes = set(self.nodes)
        if k <= 0:
            self.edges = [edge for edge in self.edges if edge["type"] != "similar"]
            return

        for view, index in indexes.items():
            if view not in weights:
                continue
            anchors = {row["ref"]: row.get("event_ref", row["ref"]) for row in index.rows}
            row_count = len(index.rows)
            candidate_refs = np.asarray([row["ref"] for row in index.rows], dtype=object)
            candidate_anchors = np.asarray([anchors[row["ref"]] for row in index.rows], dtype=object)
            candidate_anchor_valid = np.isin(candidate_anchors, list(nodes))
            for query_start in range(0, row_count, self._QUERY_BLOCK):
                query_end = min(query_start + self._QUERY_BLOCK, row_count)
                query_rows = [np.asarray(row["vector"], dtype=float) for row in index.rows[query_start:query_end]]
                query_matrix = np.stack([
                    query / norm if (norm := np.linalg.norm(query)) != 0 else np.zeros_like(query)
                    for query in query_rows
                ])
                best: list[list[tuple[int, float]]] = [[] for _ in range(query_end - query_start)]

                for candidate_start in range(0, row_count, self._CANDIDATE_BLOCK):
                    candidate_end = min(candidate_start + self._CANDIDATE_BLOCK, row_count)
                    scores = query_matrix @ index.matrix[candidate_start:candidate_end].T
                    for local_query, row_index in enumerate(range(query_start, query_end)):
                        row = index.rows[row_index]
                        origin = anchors[row["ref"]]
                        if origin not in nodes:
                            continue
                        candidates = best[local_query]
                        tile_indices = np.arange(candidate_start, candidate_end)
                        tile_scores = scores[local_query]
                        valid = (
                            (tile_scores > 0)
                            & (candidate_refs[candidate_start:candidate_end] != row["ref"])
                            & candidate_anchor_valid[candidate_start:candidate_end]
                            & (candidate_anchors[candidate_start:candidate_end] != origin)
                        )
                        valid_indices = tile_indices[valid]
                        valid_scores = tile_scores[valid]
                        if len(valid_indices):
                            combined_indices = np.fromiter((item[0] for item in candidates), dtype=int, count=len(candidates))
                            combined_scores = np.fromiter((item[1] for item in candidates), dtype=float, count=len(candidates))
                            combined_indices = np.concatenate((combined_indices, valid_indices))
                            combined_scores = np.concatenate((combined_scores, valid_scores))
                            if len(combined_indices) > k:
                                partitioned = np.argpartition(combined_scores, len(combined_scores) - k)
                                boundary = combined_scores[partitioned[len(combined_scores) - k]]
                                tolerance = np.finfo(float).eps * max(1, index.dimension) * 4 * max(1.0, abs(boundary))
                                near_boundary = np.flatnonzero(np.abs(combined_scores - boundary) <= tolerance)
                                if len(near_boundary):
                                    exact_scores = index.matrix @ query_matrix[local_query]
                                    combined_scores[near_boundary] = exact_scores[combined_indices[near_boundary]]
                                partitioned = np.argpartition(combined_scores, len(combined_scores) - k)
                                boundary = combined_scores[partitioned[len(combined_scores) - k]]
                                above = np.flatnonzero(combined_scores > boundary)
                                tied = np.flatnonzero(combined_scores == boundary)
                                tied_order = sorted(tied, key=lambda i: (candidate_refs[combined_indices[i]], combined_indices[i]))
                                chosen = list(above) + tied_order[:k - len(above)]
                            else:
                                chosen = list(range(len(combined_indices)))
                            candidates[:] = sorted(
                                ((int(combined_indices[i]), float(combined_scores[i])) for i in chosen),
                                key=lambda item: (-item[1], candidate_refs[item[0]], item[0]),
                            )

                for local_query, row_index in enumerate(range(query_start, query_end)):
                    row = index.rows[row_index]
                    origin = anchors[row["ref"]]
                    if origin not in nodes:
                        continue
                    for candidate_index, raw_score in best[local_query]:
                        score = float(np.clip(raw_score, -1, 1))
                        candidate = index.rows[candidate_index]
                        directed.setdefault((origin, anchors[candidate["ref"]]), {})[view] = score

        pairs: dict[tuple[str, str], dict[str, float]] = {}
        for (left, right), scores in directed.items():
            if mutual and (right, left) not in directed:
                continue
            pair = tuple(sorted((left, right)))
            target = pairs.setdefault(pair, {})
            for view, score in scores.items():
                target[view] = max(target.get(view, 0), score)
        candidates = []
        for (left, right), scores in pairs.items():
            denominator = sum(weights[view] for view in scores)
            score = sum(weights[view] * value for view, value in scores.items()) / denominator
            if score >= threshold:
                candidates.append({
                    "edge_id": f"sim_{digest([left, right])[:24]}",
                    "from_ref": left,
                    "to_ref": right,
                    "type": "similar",
                    "direction": "undirected",
                    "weight": score,
                    "views": scores,
                    "missing_view_policy": "renormalize_available",
                    "status": "algorithmic_candidate",
                })
        degree = {node: 0 for node in nodes}
        selected = []
        for edge in sorted(candidates, key=lambda item: (-item["weight"], item["edge_id"])):
            if degree[edge["from_ref"]] < k and degree[edge["to_ref"]] < k:
                selected.append(edge)
                degree[edge["from_ref"]] += 1
                degree[edge["to_ref"]] += 1
        self.edges = [edge for edge in self.edges if edge["type"] != "similar"] + selected
