import asyncio
import threading

import pytest

from myatri.contracts import EmbeddingBatch
from myatri.providers.local_onnx import LocalOnnxEmbeddingProvider
from warmagent.bounded_embeddings import (
    BoundedOnnxEmbeddingProvider,
    bound_index_embeddings,
)


class StubOnnxProvider(LocalOnnxEmbeddingProvider):
    def __init__(self):
        self.model = "fixture-model"
        self.model_revision = "fixture-revision"
        self.dimension = 1
        self.normalization = "l2"
        self.space_id = "fixture-space"
        self.calls = []
        self.thread_ids = []

    def embed_sync(self, texts, space_id):
        self.calls.append((list(texts), space_id))
        self.thread_ids.append(threading.get_ident())
        if space_id != self.space_id:
            raise ValueError("embedding space mismatch")
        return EmbeddingBatch(
            vectors=[[float(int(text))] for text in texts],
            space_id=self.space_id,
            model=self.model,
            model_revision=self.model_revision,
            dimension=self.dimension,
            normalization=self.normalization,
        )


def test_sync_embedding_splits_8_8_8_1_and_preserves_identity():
    original = StubOnnxProvider()
    provider = BoundedOnnxEmbeddingProvider(original)
    batch = provider.embed_sync([str(i) for i in range(25)], original.space_id)

    assert [len(texts) for texts, _ in original.calls] == [8, 8, 8, 1]
    assert [vector[0] for vector in batch.vectors] == list(map(float, range(25)))
    assert (batch.model, batch.model_revision, batch.dimension,
            batch.normalization, batch.space_id) == (
                original.model, original.model_revision, original.dimension,
                original.normalization, original.space_id)


def test_async_embedding_uses_worker_thread_and_keeps_order():
    original = StubOnnxProvider()
    provider = BoundedOnnxEmbeddingProvider(original)
    loop_thread = threading.get_ident()
    batch = asyncio.run(provider.embed([str(i) for i in range(25)], original.space_id))

    assert [len(texts) for texts, _ in original.calls] == [8, 8, 8, 1]
    assert all(thread_id != loop_thread for thread_id in original.thread_ids)
    assert [vector[0] for vector in batch.vectors] == list(map(float, range(25)))


def test_wrong_space_and_empty_input_keep_provider_semantics():
    original = StubOnnxProvider()
    provider = BoundedOnnxEmbeddingProvider(original)
    with pytest.raises(ValueError, match="space mismatch"):
        provider.embed_sync(["1"], "wrong-space")

    empty = provider.embed_sync([], original.space_id)
    assert original.calls[-1] == ([], original.space_id)
    assert empty.vectors == []
    assert empty.space_id == original.space_id


def test_index_provider_references_are_wrapped_once():
    original = StubOnnxProvider()
    index = type("Index", (), {})()
    index.provider = original
    index.providers = {"all": original, "already": BoundedOnnxEmbeddingProvider(original)}

    bound_index_embeddings(index)
    wrapper = index.provider
    assert isinstance(wrapper, BoundedOnnxEmbeddingProvider)
    assert index.providers["all"] is wrapper
    assert index.providers["already"] is not wrapper
    bound_index_embeddings(index)
    assert index.provider is wrapper
    assert index.providers["all"] is wrapper
