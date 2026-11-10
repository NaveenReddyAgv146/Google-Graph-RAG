"""Unit tests for deterministic MMR reranking."""

import numpy as np
from common.search.mmr import compute_mmr

def test_mmr_empty_input():
    res = compute_mmr(np.array([1.0, 0.0]), np.empty((0, 2)), [])
    assert res == []

def test_mmr_ranking_relevance_vs_diversity():
    # Query vector pointing along x-axis
    query = np.array([1.0, 0.0])

    # Candidate 1: Identical to query [1.0, 0.0]
    # Candidate 2: Almost identical to query [0.99, 0.01] (redundant)
    # Candidate 3: Orthogonal diverse vector [0.0, 1.0]
    candidates = ["doc1_exact", "doc2_redundant", "doc3_diverse"]
    cand_vecs = np.array([
        [1.0, 0.0],
        [0.99, 0.01],
        [0.0, 1.0],
    ])

    # When lambda = 0.3 (prioritizing diversity over redundancy):
    # doc1 is chosen first (highest relevance).
    # doc2 score = 0.3 * 1.0 - 0.7 * 1.0 = -0.4
    # doc3 score = 0.3 * 0.0 - 0.7 * 0.0 = 0.0
    # doc3 should be chosen second because doc2 is heavily penalized by similarity to doc1!
    reranked = compute_mmr(query, cand_vecs, candidates, lambda_param=0.3, top_k=2)
    assert len(reranked) == 2
    assert reranked[0] == "doc1_exact"
    assert reranked[1] == "doc3_diverse"

