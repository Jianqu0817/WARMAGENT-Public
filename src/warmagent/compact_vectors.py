"""Exact float64 vectors with metadata rows and a CSR TF-IDF build path."""
from __future__ import annotations

import heapq
import asyncio
from types import SimpleNamespace

import numpy as np
from scipy import sparse

from myatri.memory.embeddings import LocalTfidfEmbeddingProvider
from myatri.memory.vector_index import ExactVectorIndex


class MatrixRows:
    def __init__(self, matrix):
        self.matrix = matrix

    def __len__(self):
        return self.matrix.shape[0]

    def __getitem__(self, position):
        if position >= len(self):
            raise IndexError(position)
        return self.matrix[position:position + 1] if sparse.issparse(self.matrix) else self.matrix[position]


class SparseTfidfProvider(LocalTfidfEmbeddingProvider):
    async def embed(self, texts, space_id):
        # Ordinary transform() keeps the existing query/delta API. Builds and
        # aquery can pass CSR directly to the exact index without dense lists.
        if space_id != self.space_id:
            raise ValueError('Vector space mismatch')
        matrix = await asyncio.to_thread(self.vectorizer.transform, texts)
        return SimpleNamespace(vectors=MatrixRows(matrix.tocsr()),
            space_id=self.space_id, model=self.model, model_revision=self.model_revision,
            dimension=self.dimension, normalization=self.normalization)


class VectorRow(dict):
    def __init__(self, metadata, matrix, position):
        super().__init__((key, value) for key, value in metadata.items() if key != 'vector')
        self._matrix, self._position = matrix, position

    def __getitem__(self, key):
        if key == 'vector':
            row = self._matrix[self._position:self._position + 1]
            return row.toarray().ravel() if sparse.issparse(row) else row[0]
        return super().__getitem__(key)


class CompactVectorIndex(ExactVectorIndex):
    def __init__(self, space_id, dimension, rows, *, matrix=None):
        self.space_id, self.dimension = space_id, dimension
        if matrix is None:
            values = [row['vector'] for row in rows]
            matrix = sparse.vstack(values, format='csr') if values and sparse.issparse(values[0]) else np.asarray(values, dtype=np.float64).reshape((-1, dimension))
        if matrix.shape != (len(rows), dimension) or matrix.dtype != np.dtype('float64'):
            raise ValueError('Embedding matrix shape/dtype mismatch')
        if any(row['space_id'] != space_id or row['dimension'] != dimension for row in rows):
            raise ValueError('Embedding space/dimension mismatch')
        if not np.isfinite(matrix.data if sparse.issparse(matrix) else matrix).all():
            raise ValueError('Nonfinite vector')
        self.raw_matrix = matrix
        if sparse.issparse(matrix):
            norms = np.sqrt(np.asarray(matrix.multiply(matrix).sum(axis=1)).ravel())
            self.matrix = sparse.diags(np.divide(1.0, norms, out=np.zeros_like(norms), where=norms != 0)) @ matrix
            self.matrix = self.matrix.tocsr()
        else:
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            self.matrix = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms != 0)
        self.rows = [VectorRow(row, matrix, i) for i, row in enumerate(rows)]

    def search(self, vector, *, space_id, limit=5, exclude_ref=None, allowed_refs=None):
        query = vector.toarray().ravel() if sparse.issparse(vector) else np.asarray(vector, dtype=float)
        if space_id != self.space_id or query.shape != (self.dimension,):
            raise ValueError('Query vector space/dimension mismatch')
        if not np.isfinite(query).all():
            raise ValueError('Nonfinite query')
        norm = np.linalg.norm(query)
        if norm == 0 or not self.rows or limit <= 0:
            return []
        scores = np.asarray(self.matrix @ (query / norm)).ravel()
        positions = heapq.nsmallest(limit, (i for i, score in enumerate(scores) if score > 0
            and self.rows[i]['ref'] != exclude_ref and (allowed_refs is None or self.rows[i]['ref'] in allowed_refs)),
            key=lambda i: (-float(scores[i]), self.rows[i]['ref']))
        return [{'ref': self.rows[i]['ref'], 'score': float(np.clip(scores[i], -1, 1)),
            'space_id': space_id, 'view': self.rows[i]['view']} for i in positions]
