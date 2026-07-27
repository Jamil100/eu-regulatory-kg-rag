"""Question -> node linking.

Extract entity mentions from the question, resolve to node IDs via alias
lookup + Embed v4 similarity. Reuses the Phase 1 entity-resolution machinery.
"""

from __future__ import annotations


def link(question: str) -> list[str]:
    """Return resolved graph node IDs referenced by the question."""
    raise NotImplementedError
