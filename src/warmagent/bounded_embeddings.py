"""Bound ONNX embedding batches without changing their vector space."""

from __future__ import annotations

import asyncio

from myatri.contracts import EmbeddingBatch
from myatri.providers.local_onnx import LocalOnnxEmbeddingProvider


class BoundedOnnxEmbeddingProvider:
    def __init__(self, provider: LocalOnnxEmbeddingProvider, batch_size: int = 8):
        self._provider = provider
        self.batch_size = batch_size

    @property
    def model(self):
        return self._provider.model

    @property
    def model_revision(self):
        return self._provider.model_revision

    @property
    def dimension(self):
        return self._provider.dimension

    @property
    def normalization(self):
        return self._provider.normalization

    @property
    def space_id(self):
        return self._provider.space_id

    def embed_sync(self, texts: list[str], space_id: str) -> EmbeddingBatch:
        if not texts:
            return self._provider.embed_sync(texts, space_id)
        batches = [self._provider.embed_sync(texts[start:start + self.batch_size], space_id)
                   for start in range(0, len(texts), self.batch_size)]
        first = batches[0]
        vectors = [vector for batch in batches for vector in batch.vectors]
        return EmbeddingBatch(vectors=vectors, space_id=first.space_id,
                              model=first.model, model_revision=first.model_revision,
                              dimension=first.dimension, normalization=first.normalization)

    async def embed(self, texts: list[str], space_id: str) -> EmbeddingBatch:
        return await asyncio.to_thread(self.embed_sync, texts, space_id)


def bound_index_embeddings(index, batch_size: int = 8) -> None:
    """Wrap ONNX provider references on a MemoryIndex, once per provider object."""
    replacements = {}

    def bounded(provider):
        if isinstance(provider, BoundedOnnxEmbeddingProvider):
            return provider
        if not isinstance(provider, LocalOnnxEmbeddingProvider):
            return provider
        key = id(provider)
        if key not in replacements:
            replacements[key] = BoundedOnnxEmbeddingProvider(provider, batch_size)
        return replacements[key]

    index.provider = bounded(index.provider)
    index.providers = {view: bounded(provider) for view, provider in index.providers.items()}
