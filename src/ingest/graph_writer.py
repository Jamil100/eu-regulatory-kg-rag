"""Idempotent Neo4j writes.

MERGE on (type, canonical_name), never CREATE. Every relationship carries
source_chunk_id and confidence. Re-running ingestion is a no-op.
"""

from __future__ import annotations

from src.schemas import Entity, Relation


def write_entities(entities: list[Entity]) -> None:
    """MERGE entity nodes on (type, canonical_name)."""
    raise NotImplementedError


def write_relations(relations: list[Relation]) -> None:
    """MERGE relationships, attaching source_chunk_id and confidence."""
    raise NotImplementedError
