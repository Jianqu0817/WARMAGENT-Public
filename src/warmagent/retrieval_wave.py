"""Opt-in bounded propagation over already-visible recall candidates.

Scores are ranking signals, never truth probabilities. This module owns no archive
access: callers must scope candidates and edges before invoking it.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import log1p


@dataclass(frozen=True)
class WaveResult:
    order: tuple[str, ...]
    scores: dict[str, float]
    node_flow: tuple[dict, ...]
    edge_flow: tuple[dict, ...]
    cache_key: tuple[str, str, str]


@dataclass(frozen=True)
class ScopedCandidateResult:
    snapshot: str | None
    scope: str
    candidates: tuple[object, ...]
    scores: dict[str, float]
    paths: dict[str, tuple[str, ...]]
    node_flow: tuple[dict, ...]
    edge_flow: tuple[dict, ...]


_CACHE: dict[tuple[str, str, str], WaveResult] = {}


def _unit(values):
    if not values:
        return {}
    low, high = min(values.values()), max(values.values())
    return {key: (value - low) / (high - low) if high > low else 0.0
            for key, value in values.items()}


def offline_residual_structure_bonus(base_scores: dict[str, float], residual_scores: dict[str, float],
                                     structure_matches: dict[str, float], *,
                                     residual_weight: float = 0.15,
                                     structure_weight: float = 0.10) -> dict[str, float]:
    """Pure offline ablation helper; returns ranking signals, never fact probabilities."""
    if residual_weight < 0 or structure_weight < 0:
        raise ValueError("bonus weights must be nonnegative")
    refs = set(base_scores)
    base, residual, structure = _unit(base_scores), _unit(
        {ref: residual_scores.get(ref, 0.0) for ref in refs}), _unit(
        {ref: structure_matches.get(ref, 0.0) for ref in refs})
    return {ref: base[ref] + residual_weight * residual[ref] + structure_weight * structure[ref]
            for ref in refs}


def hub_suppress_wave(base_scores: dict[str, float], edges: list[dict], *, snapshot: str,
                      scope: str, direct_refs=(), hops: int = 2, decay: float = 0.35,
                      max_degree: int = 8) -> WaveResult:
    """Hub-damped, non-backtracking propagation within caller-visible graph."""
    if not 0 <= decay <= 1 or hops < 0 or max_degree < 1:
        raise ValueError("invalid wave bounds")
    refs = set(base_scores)
    normalized = []
    for edge in edges:
        a, b, kind = edge.get("from_ref"), edge.get("to_ref"), edge.get("type", "related")
        # Enforce scope/candidate visibility before any propagation.
        if a in refs and b in refs and kind != "contradicts":
            normalized.append((a, b, kind, edge.get("edge_ref")))
    signature = repr((sorted(base_scores.items()), sorted(normalized, key=repr),
                      sorted(set(direct_refs)), hops, decay, max_degree))
    key = (snapshot, scope, signature)
    if key in _CACHE:
        return _CACHE[key]
    adjacency = defaultdict(list)
    for a, b, kind, edge_ref in normalized:
        adjacency[a].append((b, kind, edge_ref))
        adjacency[b].append((a, kind, edge_ref))
    hubs = {ref: max(1.0, log1p(min(len(adjacency[ref]), max_degree))) for ref in refs}
    score = {ref: float(value) for ref, value in base_scores.items()}
    flow_nodes = [{"ref": ref, "base": score[ref], "hub_divisor": hubs[ref], "added": 0.0}
                  for ref in sorted(refs)]
    flow_edges = []
    frontier = [(ref, float(value), None) for ref, value in base_scores.items() if value > 0]
    for step in range(1, hops + 1):
        next_frontier = []
        for origin, signal, previous in frontier:
            for target, kind, edge_ref in sorted(adjacency[origin],
                    key=lambda row: (row[0], row[1], row[2] or ""))[:max_degree]:
                if target == previous:
                    continue
                amount = signal * decay / hubs[target]
                if amount <= 0:
                    continue
                score[target] += amount
                next_frontier.append((target, amount, origin))
                flow_edges.append({"from_ref": origin, "to_ref": target, "type": kind,
                                   "edge_ref": edge_ref, "step": step, "signal": amount})
                flow_nodes[[row["ref"] for row in flow_nodes].index(target)]["added"] += amount
        frontier = next_frontier
    direct = set(direct_refs)
    order = sorted(refs, key=lambda ref: (ref not in direct, -score[ref], ref))
    result = WaveResult(tuple(order), score, tuple(flow_nodes), tuple(flow_edges), key)
    if len(_CACHE) >= 128:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = result
    return result


class ScopedWaveCandidates:
    """Expand anchors only across an ACL-filtered archive graph snapshot."""
    def __init__(self, store, allowed):
        self.store, self.allowed = store, allowed
        self._snapshot = object()
        self._cache = {}

    def _allowed(self, record):
        fn = getattr(self.allowed, "allowed", None)
        return bool(fn(record) if callable(fn) else self.allowed(record))

    def _index(self, scope, snapshot):
        allowed_scope = getattr(self.allowed, "scope", scope)
        if allowed_scope != scope:
            raise ValueError("allowed visibility scope does not match requested scope")
        watermark = snapshot if snapshot is not None else self.store.snapshot()
        owner = getattr(self.allowed, "__self__", None)
        acl_key = (bool(getattr(owner, "shared_visibility", False)),
                   bool(getattr(owner, "private_channel", False)),
                   getattr(owner, "current_sender", None))
        if watermark != self._snapshot:
            self._cache.clear()
            self._snapshot = watermark
        key = (scope, watermark, acl_key)
        if key not in self._cache:
            # Apply ACL before graph construction, degree counts, or propagation.
            visible = tuple(row for row in self.store.list_records(snapshot=watermark)
                            if self._allowed(row))
            nodes = {row.ref: row for row in visible if row.record_kind in {"event", "theme"}}
            edges = []
            for row in visible:
                if row.record_kind != "edge":
                    continue
                a, b, kind = (row.payload.get("from_ref"), row.payload.get("to_ref"),
                              row.payload.get("type", "related"))
                if a in nodes and b in nodes:
                    edges.append({"from_ref": a, "to_ref": b, "type": kind, "edge_ref": row.ref})
            self._cache[key] = (nodes, edges)
            if len(self._cache) > 4:
                self._cache.pop(next(iter(self._cache)))
        return watermark, *self._cache[key]

    def expand(self, anchors, scope, snapshot=None, max_candidates=20, *, hops=2, decay=0.35):
        if max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        if hops < 0 or hops > 4:
            raise ValueError("wave hops must be between 0 and 4")
        max_candidates = min(max_candidates, 100)
        watermark, nodes, edges = self._index(scope, snapshot)
        base = {ref: 0.0 for ref in nodes}
        direct = set()
        supplied = anchors if isinstance(anchors, dict) else {ref: 1.0 for ref in anchors}
        for ref, score in supplied.items():
            if ref in nodes:
                base[ref] = max(float(score), 0.0)
                direct.add(ref)
        scoped_edges = [edge for edge in edges if edge["type"] != "contradicts"]
        wave = hub_suppress_wave(base, scoped_edges, snapshot=watermark or "empty",
                                 scope=scope, direct_refs=direct, hops=hops, decay=decay)
        reachable = [ref for ref in wave.order if ref in direct or wave.scores[ref] > 0]
        chosen_refs = reachable[:max_candidates]
        selected = tuple(nodes[ref] for ref in chosen_refs)
        adjacency = {}
        for edge in scoped_edges:
            adjacency.setdefault(edge["from_ref"], []).append(edge["to_ref"])
            adjacency.setdefault(edge["to_ref"], []).append(edge["from_ref"])
        paths = {}
        for ref in chosen_refs:
            if ref in direct:
                paths[ref] = (ref,)
                continue
            queue = [(anchor, (anchor,)) for anchor in sorted(direct)]
            found = None
            while queue and found is None:
                current, path = queue.pop(0)
                if current == ref:
                    found = path
                    break
                if len(path) > hops:
                    continue
                for neighbor in sorted(adjacency.get(current, [])):
                    if neighbor not in path:
                        queue.append((neighbor, (*path, neighbor)))
            if found:
                paths[ref] = found
        return ScopedCandidateResult(watermark, scope, selected,
            {ref: wave.scores[ref] for ref in chosen_refs}, paths,
            tuple(row for row in wave.node_flow if row["ref"] in chosen_refs),
            tuple(row for row in wave.edge_flow if row["from_ref"] in chosen_refs and row["to_ref"] in chosen_refs))

