"""Citation validation.

Assert every cited document ID was actually in the retrieved set. On failure,
regenerate once, then fail loudly. Count events for the README rejection rate.
"""

from __future__ import annotations

from src.schemas import Citation


def validate(citations: list[Citation], retrieved_ids: set[str]) -> bool:
    """Return True iff every cited chunk_id is in the retrieved set."""
    raise NotImplementedError
