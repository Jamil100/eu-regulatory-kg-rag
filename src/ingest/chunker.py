"""Paragraph-level chunker.

Produces deterministic, human-readable chunk IDs like `aia-art26-para1`.
"""

from __future__ import annotations

from src.schemas import Chunk


def make_chunk_id(regulation: str, article: str, paragraph: str) -> str:
    """Deterministic, human-readable chunk id, e.g. `aia-art26-para1`."""
    raise NotImplementedError


def chunk(nodes: list[dict]) -> list[Chunk]:
    """Split parsed structure at the paragraph level into Chunk records."""
    raise NotImplementedError
