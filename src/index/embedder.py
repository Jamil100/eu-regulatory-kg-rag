"""Embed v4 embedding of chunks into pgvector.

input_type="search_document", int8 embeddings. Supports Matryoshka-truncated
dimensions (1536 vs 512) for the dimension experiment.
"""

from __future__ import annotations

from src.schemas import Chunk


def embed_chunks(chunks: list[Chunk], dim: int = 1536) -> None:
    """Embed and upsert chunks into the pgvector `chunks` table."""
    raise NotImplementedError


def embed_query(text: str, dim: int = 1536) -> list[float]:
    """Embed a query with input_type='search_query'."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit("TODO: wire up embedder CLI")
