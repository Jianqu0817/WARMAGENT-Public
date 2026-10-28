"""Explicit full-rebuild adapter: this remains the correctness oracle for later synchronization seams."""
from __future__ import annotations

import json
import uuid

from myatri.contracts import EmbeddingBatch, digest
from myatri.memory.clustering import cluster
from myatri.memory.embeddings import LocalTfidfEmbeddingProvider, cache_key, text_hash
from myatri.memory.index import MemoryIndex
from myatri.memory.themes import build_themes
from myatri.memory.vector_index import ExactVectorIndex
from myatri.storage.archive import _atomic_write, _json
from warmagent.index_graph import FastMemoryGraph


class GraphMemoryIndex(MemoryIndex):
    """Use FastMemoryGraph during the oracle rebuild; later updates may add a sync seam."""

    def _finish_rebuild(self, prepared: dict, batches: dict[str, EmbeddingBatch]) -> str:
        snapshot = prepared["snapshot"]
        records, record_map, cache = prepared["records"], prepared["record_map"], prepared["cache"]
        indexes: dict[str, ExactVectorIndex] = {}
        providers = {}
        provider_states: dict[str, dict] = {}
        hits = 0
        for plan in prepared["plans"]:
            view, items, texts = plan["view"], plan["items"], plan["texts"]
            provider, space_id = plan["provider"], plan["space_id"]
            cached, missing_positions = plan["cached"], plan["missing_positions"]
            if missing_positions:
                batch = batches[view]
                if batch.space_id != space_id or len(batch.vectors) != len(missing_positions):
                    raise ValueError("Embedding provider returned another space or incomplete batch")
                if batch.normalization != "l2":
                    raise ValueError("Exact cosine baseline requires declared l2 normalization")
                for position, vector in zip(missing_positions, batch.vectors, strict=True):
                    cached[position] = {"vector": vector, "space_id": batch.space_id, "view": view,
                                        "model": batch.model, "model_revision": batch.model_revision,
                                        "dimension": batch.dimension, "normalization": batch.normalization,
                                        "text_hash": text_hash(texts[position]), "cache_key": cache_key(texts[position], view, batch)}
                    if plan["stable_identity"]:
                        cache[plan["lookup_keys"][position]] = cached[position]
            hits += len(items) - len(missing_positions)
            rows = []
            metadata_identity = set()
            for (record, _), entry in zip(items, cached, strict=True):
                assert entry is not None
                event_refs = [ref for ref in record.source_refs if ref in record_map and record_map[ref].record_kind == "event"]
                anchor = event_refs[0] if view == "feeling" and len(event_refs) == 1 else record.ref
                rows.append({**entry, "ref": record.ref, "event_ref": anchor, "record_id": record.record_id, "revision": record.revision})
                metadata_identity.add((entry["space_id"], entry["model"], entry["model_revision"], entry["dimension"], entry["normalization"]))
            if len(metadata_identity) != 1:
                raise ValueError("Provider/model revision changed within one vector space")
            indexes[view] = ExactVectorIndex(space_id, rows[0]["dimension"], rows)
            providers[view] = provider
            provider_states[view] = provider.state() if isinstance(provider, LocalTfidfEmbeddingProvider) else {"type": "external", "space_id": space_id, "model": rows[0]["model"], "model_revision": rows[0]["model_revision"]}
        generation = f"index_{uuid.uuid4().hex}"
        graph = FastMemoryGraph([r.ref for r in records if r.record_kind == "event"], generation=generation)
        graph.upsert_similarity_edges(indexes, k=self.k, threshold=self.threshold)
        # Old-version edges are retained in the archive; current graph only
        # includes endpoints visible in this generation's current revision set.
        edges = [r for r in records if r.record_kind == "edge" and r.payload.get("from_ref") in record_map and r.payload.get("to_ref") in record_map]
        # Index membership is latest-only; evidence is an immutable pinned
        # revision and remains valid after its record gains a newer revision.
        graph.apply_semantic_edges(edges, set(snapshot.records))
        clustered = cluster(graph, seed=self.seed)
        themes, lineage = build_themes(clustered, graph, record_map, prepared["previous_themes"])
        for theme_id, theme in themes.items():
            model_record = next((record for record in records if record.record_kind == "theme" and record.record_id == theme_id), None)
            if model_record is not None and model_record.payload.get("material_hash") == theme["material_hash"]:
                theme.update(name=model_record.payload.get("name") or model_record.payload.get("label") or theme["name"],
                             summary=model_record.body_markdown, naming_source="model" if model_record.author_kind == "model" else "imported",
                             needs_summary=False, summary_ref=model_record.ref)
            elif model_record is not None:
                theme.update(name=f"暂定主题 {list(themes).index(theme_id) + 1}", summary="",
                             naming_source="algorithmic_placeholder", needs_summary=True)
        clustered.lineage = lineage
        limitations = ["本地 char-ngram TF-IDF 是词面向量基线，不是神经语义 Embedding；否定与跨话题语义能力有限。"] if self.provider is None or isinstance(self.provider, LocalTfidfEmbeddingProvider) else []
        limitations += ["群落主题仍为 provisional；算法占位标签不代表 A3 模型语义摘要已验证。", "感受视角缺失时跳过并重新归一化可用视角权重；不会伪造感受。"]
        state = {"schema_version": "1.0", "agent_id": self.store.agent_id, "user_id": self.store.user_id,
                 "generation": generation, "watermark": snapshot.watermark,
                 "archive_sequence": snapshot.sequence, "providers": provider_states,
                 "indexes": {view: {"space_id": index.space_id, "dimension": index.dimension, "rows": index.rows} for view, index in indexes.items()},
                 "graph": graph.state(), "clusters": clustered.model_dump(mode="json"), "themes": themes,
                 "embedding_cache": cache, "cache_hits": hits, "limitations": limitations,
                 "params": {"k": self.k, "threshold": self.threshold, "seed": self.seed}}
        checksum = digest(state)
        # Large immutable generation writes do not hold the archive writer.
        _atomic_write(self.path / "generations" / f"{generation}.json", _json({"data": state, "sha256": checksum}))
        published = False
        with self.store._writer():
            pointer_path = self.path / "current.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8")) if pointer_path.exists() else None
            # Locks make each publication atomic; comparing archive sequence
            # additionally prevents a slow older build from undoing a newer one.
            current_sequence = self._pointer_sequence(pointer) if pointer else -1
            if snapshot.sequence >= current_sequence:
                _atomic_write(pointer_path, _json(self._pointer(state, checksum)))
                self._install(state, indexes=indexes, providers=providers)
                published = True
        if not published:
            self._load_current_monotone()
        return self.capture_state()["generation"]
