"""Recall measurement harness.

Measures recall@10 on ~20 labeled queries. Used for the dimension experiment
(1536 vs Matryoshka-512) and HNSW ef_search tuning.
"""

from __future__ import annotations


def recall_at_k(labeled_queries: list[dict], k: int = 10, dim: int = 1536) -> float:
    """Compute recall@k for the given embedding dimension."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit("TODO: run recall harness")
