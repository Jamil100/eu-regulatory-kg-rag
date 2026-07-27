"""Vector retrieval path: Embed v4 query -> HNSW top-50.

Feeds the reranker. Graph retrieval lives in cypher_templates + entity_linker.
"""

from __future__ import annotations

from src.schemas import Chunk


def retrieve(question: str, top_k: int = 50) -> list[Chunk]:
    """HNSW top-k retrieval over pgvector."""
    raise NotImplementedError
