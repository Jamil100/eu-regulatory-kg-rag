"""Rerank 3.5 cross-encoder: reorder top-50 -> top-5 passages."""

from __future__ import annotations

from src.schemas import Chunk


def rerank(question: str, candidates: list[Chunk], top_n: int = 5) -> list[Chunk]:
    """Rerank retrieved candidates with Rerank 3.5."""
    raise NotImplementedError
