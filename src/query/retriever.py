"""Vector retrieval path: Embed v4 query -> HNSW top-50.

Feeds the reranker. Graph retrieval lives in cypher_templates + entity_linker.
"""

from __future__ import annotations

from src.schemas import ContextDoc


def retrieve(question: str, top_k: int = 50) -> list[ContextDoc]:
    """HNSW top-k retrieval over pgvector.

    Returns ContextDoc, not Chunk (ADR-0011): a similarity score has to travel
    with each result for the reranker to have anything to improve on, and Chunk
    is extra="forbid" with no score field on purpose -- see
    docs/adr/adr-0011-context-document-model.md.
    """
    raise NotImplementedError
