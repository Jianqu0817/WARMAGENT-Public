import sqlite3

import pytest

from myatri.contracts import EmbeddingBatch
from myatri.memory.embeddings import LocalTfidfEmbeddingProvider
from warmagent.embedding_cache import (
    CachedTfidfProvider,
    cache_provider,
)


class StubProvider:
    def __init__(self, *, model="stub", revision="r1", space="s1", dimension=2):
        self.model = model
        self.model_revision = revision
        self.dimension = dimension
        self.normalization = "l2"
        self.space_id = space
        self.calls = []
        self.invalid = False

    def embed_sync(self, texts, space_id):
        self.calls.append((list(texts), space_id))
        vectors = [[float(len(text)), *([1.0] * (self.dimension - 1))] for text in texts]
        if self.invalid and vectors:
            vectors[0][0] = float("nan")
        return EmbeddingBatch(vectors=vectors, space_id=space_id,
                              model=self.model, model_revision=self.model_revision,
                              dimension=self.dimension, normalization=self.normalization)


def test_cache_persists_and_deduplicates_repeated_text(tmp_path):
    path = tmp_path / "indexes" / "incremental-vectors.sqlite3"
    source = StubProvider()
    cached = cache_provider(source, path, "event")
    batch = cached.embed_sync(["alpha", "alpha", "beta"], "s1")
    assert source.calls == [(["alpha", "beta"], "s1")]
    assert batch.vectors[0] == batch.vectors[1]

    second = cache_provider(StubProvider(), path, "event")
    hit = second.transform(["beta", "alpha"], "s1")
    assert hit.vectors == [[4.0, 1.0], [5.0, 1.0]]
    assert second._provider.calls == []


def test_cache_isolated_by_space_view_and_provider_identity(tmp_path):
    path = tmp_path / "vectors.sqlite3"
    original = StubProvider()
    cache_provider(original, path, "event").embed_sync(["same"], "s1")

    for provider, view, space in (
        (StubProvider(), "feeling", "s1"),
        (StubProvider(space="s2"), "event", "s2"),
        (StubProvider(model="other"), "event", "s1"),
        (StubProvider(revision="r2"), "event", "s1"),
    ):
        cache_provider(provider, path, view).embed_sync(["same"], space)
        assert len(provider.calls) == 1


def test_invalid_vectors_are_not_cached(tmp_path):
    path = tmp_path / "vectors.sqlite3"
    source = StubProvider()
    source.invalid = True
    cached = cache_provider(source, path, "event")
    with pytest.raises(ValueError, match="embedding dimensions/non-finite values"):
        cached.embed_sync(["bad"], "s1")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0


def test_cached_tfidf_preserves_core_type_and_serializable_state(tmp_path):
    original = LocalTfidfEmbeddingProvider("event").fit(["猫咪喜欢阳光", "狗狗喜欢散步"])
    cached = cache_provider(original, tmp_path / "vectors.sqlite3", "event")
    assert isinstance(cached, LocalTfidfEmbeddingProvider)
    assert isinstance(cached, CachedTfidfProvider)
    batch = cached.transform(["猫咪喜欢阳光"], cached.space_id)
    assert len(batch.vectors) == 1
    state = cached.state()
    restored = LocalTfidfEmbeddingProvider.from_state(state)
    assert restored.space_id == original.space_id
    assert restored.state() == state
    assert cache_provider(cached, tmp_path / "vectors.sqlite3", "event") is cached


def test_cache_enforces_fifo_entry_limit(tmp_path):
    path = tmp_path / "vectors.sqlite3"
    source = StubProvider()
    cached = cache_provider(source, path, "event", max_entries=2)
    cached.embed_sync(["one", "two", "three"], "s1")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 2
    cached.embed_sync(["one"], "s1")
    assert source.calls[-1][0] == ["one"]


def test_empty_tfidf_batch_skips_transform_and_rejects_wrong_space(tmp_path):
    original = LocalTfidfEmbeddingProvider("event").fit(["猫咪喜欢阳光", "狗狗喜欢散步"])
    cached = cache_provider(original, tmp_path / "vectors.sqlite3", "event")
    empty = cached.transform([], cached.space_id)
    assert empty.vectors == []
    assert empty.space_id == cached.space_id
    with pytest.raises(ValueError, match="space mismatch"):
        cached.transform(["猫咪"], "wrong-space")

    connection = cached._cache._connect()
    with connection as db:
        db.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        db.execute("SELECT 1")


def test_cache_byte_budget_trims_fifo_and_skips_oversized_vectors(tmp_path):
    path = tmp_path / "vectors.sqlite3"
    source = StubProvider()
    cached = cache_provider(source, path, "event", max_entries=10, max_bytes=32)
    cached.embed_sync(["one", "two", "tri"], "s1")
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT COUNT(*), SUM(LENGTH(vector)) FROM vectors").fetchone()
        assert rows == (2, 32)
    cached.embed_sync(["one"], "s1")
    assert source.calls[-1][0] == ["one"]

    large = cache_provider(StubProvider(dimension=3), tmp_path / "large.sqlite3",
                           "event", max_bytes=16)
    result = large.embed_sync(["kept-for-caller"], "s1")
    assert result.vectors == [[15.0, 1.0, 1.0]]
    with sqlite3.connect(large.path) as db:
        assert db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
