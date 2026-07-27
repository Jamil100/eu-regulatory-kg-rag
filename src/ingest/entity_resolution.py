"""Entity resolution: normalize -> exact match -> Embed v4 similarity -> merge/create.

Threshold starts at 0.90, tuned on ~30 hand-labeled pairs.
"""

from __future__ import annotations

from src.schemas import Entity

SIMILARITY_THRESHOLD = 0.90


def normalize(name: str) -> str:
    """Lowercase, strip punctuation, expand known abbreviations (AIA -> AI Act)."""
    raise NotImplementedError


def resolve(entity: Entity, existing: list[Entity]) -> Entity:
    """Resolve a candidate entity against existing canonical names."""
    raise NotImplementedError
