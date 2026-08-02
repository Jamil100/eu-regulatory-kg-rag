"""Rerank 3.5 cross-encoder: reorder top-50 -> top-5 passages."""

from __future__ import annotations

from src.schemas import ContextDoc


def rerank(question: str, candidates: list[ContextDoc], top_n: int = 5) -> list[ContextDoc]:
    """Rerank retrieved candidates with Rerank 3.5.

    ContextDoc, not Chunk (ADR-0011) -- the input already carries retrieve()'s
    similarity score, and the output's score should become the rerank score,
    not silently retain a similarity number a caller could mistake for it.
    """
    raise NotImplementedError
