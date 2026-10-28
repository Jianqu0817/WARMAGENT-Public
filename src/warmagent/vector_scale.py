"""Offline exact/coarse vector scale experiment; not wired into production indexes."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import numpy as np


@dataclass(frozen=True)
class EmbeddingSpaceSignature:
    model: str
    revision: str
    dimension: int
    normalization: str
    space_id: str

    @property
    def key(self):
        return (self.model, self.revision, self.dimension, self.normalization, self.space_id)


@dataclass(frozen=True)
class VectorGeneration:
    generation: str
    signature: EmbeddingSpaceSignature
    refs: tuple[str, ...]
    text_hashes: tuple[str, ...]
    vectors: np.ndarray
    coarse: np.ndarray


class VectorScaleAdapter:
    """Float32 coarse candidate selection with float64 exact reranking."""
    def __init__(self, signature: EmbeddingSpaceSignature):
        if signature.dimension < 1:
            raise ValueError("dimension must be positive")
        self.signature = signature
        self._vector_cache = {}
        self._generations = {}
        self._active_generation = None
        self.hash_reuse_count = 0

    @property
    def active_generation(self):
        return self._active_generation

    def build_generation(self, generation, rows):
        if not generation:
            raise ValueError("generation is required")
        refs, hashes, vectors, staged = [], [], [], {}
        for row in rows:
            ref = row["ref"]
            text_hash = row.get("text_hash") or sha256(row.get("text", "").encode("utf-8")).hexdigest()
            key = (self.signature.key, text_hash)
            vector = self._vector_cache.get(key)
            if vector is None:
                vector = np.asarray(row["vector"], dtype=np.float64)
                staged[key] = vector.copy()
            else:
                self.hash_reuse_count += 1
            if vector.shape != (self.signature.dimension,) or not np.isfinite(vector).all():
                raise ValueError("vector dimension or values do not match embedding space")
            refs.append(ref)
            hashes.append(text_hash)
            vectors.append(vector)
        if len(set(refs)) != len(refs):
            raise ValueError("duplicate ref in generation")
        exact = np.asarray(vectors, dtype=np.float64).reshape((-1, self.signature.dimension))
        # Publish only after every row passes validation and both matrices are complete.
        coarse = exact.astype(np.float32)
        exact.setflags(write=False)
        coarse.setflags(write=False)
        snapshot = VectorGeneration(generation, self.signature, tuple(refs), tuple(hashes), exact, coarse)
        self._vector_cache.update(staged)
        self._generations[generation] = snapshot
        self._active_generation = generation
        return snapshot

    def query(self, vector, limit=10, candidate_limit=100, *, generation=None):
        selected_generation = generation or self._active_generation
        if selected_generation not in self._generations:
            raise KeyError("unknown vector generation")
        snapshot = self._generations[selected_generation]
        if snapshot.signature.key != self.signature.key:
            raise ValueError("generation embedding-space signature mismatch")
        if limit < 1 or candidate_limit < 1:
            raise ValueError("limits must be positive")
        query = np.asarray(vector, dtype=np.float64)
        if query.shape != (self.signature.dimension,) or not np.isfinite(query).all():
            raise ValueError("query vector dimension or values are invalid")
        coarse_scores = snapshot.coarse @ query.astype(np.float32)
        count = min(candidate_limit, len(snapshot.refs))
        candidate_indexes = np.argsort(-coarse_scores, kind="stable")[:count]
        exact_scores = snapshot.vectors[candidate_indexes] @ query
        reranked = sorted(((float(score), snapshot.refs[int(index)])
                           for score, index in zip(exact_scores, candidate_indexes, strict=True)),
                          key=lambda item: (-item[0], item[1]))
        return [{"ref": ref, "score": score, "score_kind": "exact_original_precision"}
                for score, ref in reranked[:limit]]
