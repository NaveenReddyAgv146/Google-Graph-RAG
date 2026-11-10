"""Deterministic Maximal Marginal Relevance (MMR) reranking engine."""

from typing import List, TypeVar
import numpy as np

T = TypeVar("T")

def compute_mmr(
    query_embedding: np.ndarray,
    candidate_embeddings: np.ndarray,
    candidate_items: List[T],
    lambda_param: float = 0.7,
    top_k: int = 5
) -> List[T]:
    """
    Computes deterministic Maximal Marginal Relevance (MMR) ranking.
    
    Formula:
        MMR = argmax_{d_i in R \\ S} [ lambda * Sim_1(d_i, q) - (1 - lambda) * max_{d_j in S} Sim_2(d_i, d_j) ]
        
    Args:
        query_embedding: (D,) 1D normalized query vector.
        candidate_embeddings: (N, D) 2D normalized candidate vectors.
        candidate_items: List of N candidate objects matching candidate_embeddings.
        lambda_param: Diversity tradeoff parameter [0.0 = max diversity, 1.0 = max relevance].
        top_k: Number of reranked items to return.
        
    Returns:
        List of reranked items of length min(top_k, N).
    """
    if len(candidate_items) == 0:
        return []
    if len(candidate_items) <= top_k and lambda_param == 1.0:
        return candidate_items

    # Normalize vectors to ensure cosine similarity equals dot product
    q_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
    c_norms = candidate_embeddings / (np.linalg.norm(candidate_embeddings, axis=1, keepdims=True) + 1e-10)

    # Compute query similarities: Sim_1(d_i, q)
    query_sims = np.dot(c_norms, q_norm)

    selected_indices: List[int] = []
    unselected_indices = list(range(len(candidate_items)))

    # Select candidate with highest relevance to query as first item
    first_choice = int(np.argmax(query_sims))
    selected_indices.append(first_choice)
    unselected_indices.remove(first_choice)

    # Iteratively select candidates balancing relevance and diversity
    while len(selected_indices) < min(top_k, len(candidate_items)):
        best_score = -float("inf")
        best_idx = -1

        selected_vectors = c_norms[selected_indices]  # (S, D)

        for candidate_idx in unselected_indices:
            cand_vector = c_norms[candidate_idx]  # (D,)
            sim_to_query = query_sims[candidate_idx]

            # max_{d_j in S} Sim_2(d_i, d_j)
            sim_to_selected = np.max(np.dot(selected_vectors, cand_vector))

            mmr_score = lambda_param * sim_to_query - (1.0 - lambda_param) * sim_to_selected

            if mmr_score > best_score:
                best_score = mmr_score
                best_idx = candidate_idx

        selected_indices.append(best_idx)
        unselected_indices.remove(best_idx)

    return [candidate_items[idx] for idx in selected_indices]

