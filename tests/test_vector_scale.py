import pytest

from warmagent.vector_scale import EmbeddingSpaceSignature, VectorScaleAdapter


def test_coarse_candidates_are_reranked_with_original_precision_and_hash_reused():
    signature = EmbeddingSpaceSignature("synthetic", "r1", 2, "none", "space-a")
    adapter = VectorScaleAdapter(signature)
    rows = [{"ref": "lower", "text": "same", "vector": [1.0, 0.0]},
            {"ref": "higher", "text": "other", "vector": [1.00000005, 0.0]}]
    adapter.build_generation("g1", rows)
    result = adapter.query([1.0, 0.0], limit=2, candidate_limit=2)
    assert [row["ref"] for row in result] == ["higher", "lower"]
    adapter.build_generation("g2", [{"ref": "renamed", "text": "same", "vector": [99.0, 99.0]}])
    assert adapter.hash_reuse_count == 1
    assert adapter.query([1.0, 0.0], generation="g2")[0]["ref"] == "renamed"


def test_generation_publication_is_atomic_and_space_signature_is_explicit():
    adapter = VectorScaleAdapter(EmbeddingSpaceSignature("synthetic", "r1", 2, "l2", "space-a"))
    adapter.build_generation("good", [{"ref": "x", "text": "x", "vector": [1.0, 0.0]}])
    with pytest.raises(ValueError):
        adapter.build_generation("bad", [{"ref": "y", "text": "y", "vector": [1.0]}])
    assert adapter.active_generation == "good"
    assert adapter.query([1.0, 0.0])[0]["ref"] == "x"
    other_space = VectorScaleAdapter(EmbeddingSpaceSignature("synthetic", "r2", 2, "l2", "space-b"))
    other_space.build_generation("g", [{"ref": "y", "text": "x", "vector": [1.0, 0.0]}])
    assert other_space.hash_reuse_count == 0
