"""Grounded answer generation with Command A + documents parameter.

Returns native citation spans mapping answer text to source documents.
"""

from __future__ import annotations

from src.schemas import Citation


def generate(question: str, documents: list[dict]) -> tuple[str, list[Citation]]:
    """Generate a grounded answer and native citations."""
    raise NotImplementedError
