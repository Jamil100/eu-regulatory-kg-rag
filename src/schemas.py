"""Shared Pydantic models: ontology-constrained extraction + API contracts.

The ontology (12 entity types, 13 relationship types) is defined once in
src/ingest/extract.py and re-exported here, so there is a single source of
truth. Import Entity/Relationship/Extraction from either module.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.ingest.extract import (
    Entity,
    EntityType,
    Extraction,
    RelationType,
    Relationship,
)

__all__ = [
    "Entity",
    "EntityType",
    "Extraction",
    "RelationType",
    "Relationship",
    "Chunk",
    "Citation",
    "AskRequest",
    "AskResponse",
]


class Chunk(BaseModel):
    chunk_id: str
    regulation: str
    chapter: str | None = None
    article: str | None = None
    paragraph: str | None = None
    text: str


class Citation(BaseModel):
    chunk_id: str
    start: int
    end: int
    text: str


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    route: Literal["graph", "vector", "both"]
    latency_ms: float
    cost_usd: float
