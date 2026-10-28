"""Persistent, content-addressed cache for embedding vectors."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import math
from pathlib import Path
import sqlite3
import struct
import time
from typing import Any

from myatri.contracts import EmbeddingBatch, digest
from myatri.memory.embeddings import LocalTfidfEmbeddingProvider, text_hash


class CachedEmbeddingProvider:
    DEFAULT_MAX_BYTES = 64 * 1024 * 1024

    def __init__(self, provider: Any, path: Path, view: str, max_entries: int = 10000,
                 max_bytes: int = DEFAULT_MAX_BYTES):
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self._provider = provider
        self.path = Path(path)
        self.view = str(view)
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL, dimension INTEGER NOT NULL, created REAL NOT NULL)")

    def __getattr__(self, name):
        return getattr(self._provider, name)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _validate(batch: EmbeddingBatch, expected: tuple, count: int) -> None:
        identity = (batch.space_id, batch.model, batch.model_revision, batch.dimension, batch.normalization)
        if identity != expected or type(batch.dimension) is not int or batch.dimension < 1:
            raise ValueError("Embedding provider metadata mismatch")
        if len(batch.vectors) != count:
            raise ValueError("Embedding provider returned incomplete batch")
        for vector in batch.vectors:
            if len(vector) != batch.dimension or any(not math.isfinite(float(value)) for value in vector):
                raise ValueError("Embedding provider returned invalid vector")

    def embed_sync(self, texts: list[str], space_id: str) -> EmbeddingBatch:
        texts = list(texts)
        if space_id != self.space_id:
            raise ValueError("Embedding space mismatch")
        if (not isinstance(self.model, str) or not self.model or
                not isinstance(self.model_revision, str) or not self.model_revision or
                type(self.dimension) is not int or self.dimension < 1 or
                not isinstance(self.normalization, str) or not self.normalization):
            raise ValueError("Embedding provider metadata is incomplete")
        if not texts:
            return EmbeddingBatch(vectors=[], space_id=space_id, model=self.model,
                                  model_revision=self.model_revision,
                                  dimension=self.dimension, normalization=self.normalization)

        expected = (space_id, self.model, self.model_revision, self.dimension, self.normalization)
        keys = [digest({"space": space_id, "view": self.view, "text_hash": text_hash(text),
                        "model": self.model, "revision": self.model_revision,
                        "dimension": self.dimension, "normalization": self.normalization}) for text in texts]
        unique_keys = list(dict.fromkeys(keys))
        found = {}
        with self._connect() as db:
            for start in range(0, len(unique_keys), 500):
                chunk = unique_keys[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                for key, blob, dimension in db.execute(
                        f"SELECT key, vector, dimension FROM vectors WHERE key IN ({marks})", chunk):
                    if dimension != self.dimension or len(blob) != dimension * 8:
                        db.execute("DELETE FROM vectors WHERE key=?", (key,))
                        continue
                    vector = list(struct.unpack("<" + "d" * dimension, blob))
                    if all(math.isfinite(value) for value in vector):
                        found[key] = vector
                    else:
                        db.execute("DELETE FROM vectors WHERE key=?", (key,))

            missing = [key for key in unique_keys if key not in found]
            if missing:
                text_for_key = {}
                for key, text in zip(keys, texts, strict=True):
                    text_for_key.setdefault(key, text)
                ordered_texts = [text_for_key[key] for key in missing]
                method = getattr(self._provider, "transform", None) or self._provider.embed_sync
                fresh = method(ordered_texts, space_id)
                self._validate(fresh, expected, len(missing))
                now = time.time()
                rows = []
                for key, vector in zip(missing, fresh.vectors, strict=True):
                    normalized = [float(value) for value in vector]
                    found[key] = normalized
                    blob = struct.pack("<" + "d" * self.dimension, *normalized)
                    if len(blob) <= self.max_bytes:
                        rows.append((key, blob, self.dimension, now))
                if rows:
                    db.executemany("INSERT OR REPLACE INTO vectors(key,vector,dimension,created) VALUES(?,?,?,?)", rows)
                    count, size = db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(vector)), 0) FROM vectors").fetchone()
                    if count > self.max_entries or size > self.max_bytes:
                        oldest = db.execute("SELECT key, LENGTH(vector) FROM vectors ORDER BY created ASC, rowid ASC").fetchall()
                        for key, length in oldest:
                            if count <= self.max_entries and size <= self.max_bytes:
                                break
                            db.execute("DELETE FROM vectors WHERE key=?", (key,))
                            count -= 1
                            size -= length

        return EmbeddingBatch(vectors=[found[key] for key in keys], space_id=space_id,
                              model=self.model, model_revision=self.model_revision,
                              dimension=self.dimension, normalization=self.normalization)

    transform = embed_sync

    async def embed(self, texts: list[str], space_id: str) -> EmbeddingBatch:
        return await asyncio.to_thread(self.embed_sync, texts, space_id)


class CachedTfidfProvider(LocalTfidfEmbeddingProvider):
    def __init__(self, provider: LocalTfidfEmbeddingProvider, path: Path, view: str,
                 max_entries: int = 10000, max_bytes: int = CachedEmbeddingProvider.DEFAULT_MAX_BYTES):
        self.__dict__.update(provider.__dict__)
        self._cache = CachedEmbeddingProvider(provider, path, view, max_entries, max_bytes)
        self._original = provider

    def fit(self, texts: list[str]):
        self._original.fit(texts)
        self.__dict__.update(self._original.__dict__)
        return self

    def transform(self, texts: list[str], space_id: str | None = None) -> EmbeddingBatch:
        return self._cache.embed_sync(texts, space_id or self.space_id)

    async def embed(self, texts: list[str], space_id: str) -> EmbeddingBatch:
        return await self._cache.embed(texts, space_id)

    def state(self) -> dict[str, Any]:
        return self._original.state()


def cache_provider(provider: Any, path: Path, view: str, max_entries: int = 10000,
                   max_bytes: int = CachedEmbeddingProvider.DEFAULT_MAX_BYTES):
    if isinstance(provider, CachedTfidfProvider):
        if provider._cache.path == Path(path) and provider._cache.view == str(view):
            return provider
        provider = provider._original
    elif isinstance(provider, CachedEmbeddingProvider):
        if provider.path == Path(path) and provider.view == str(view):
            return provider
        provider = provider._provider
    if isinstance(provider, LocalTfidfEmbeddingProvider):
        return CachedTfidfProvider(provider, path, view, max_entries, max_bytes)
    return CachedEmbeddingProvider(provider, path, view, max_entries, max_bytes)
