"""Shared Pydantic models: ontology-constrained extraction + API contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EntityType = Literal[
    "Regulation",
    "Article",
    "Annex",
    "ActorRole",
    "Obligation",
    "RiskCategory",
    "SystemType",
    "Authority",
]

RelationType = Literal[
    "DEFINED_IN",
    "IMPOSES",
    "APPLIES_TO",
    "CLASSIFIED_AS",
    "LISTED_IN",
    "REFERENCES",
    "ENFORCED_BY",
    "PENALIZED_UNDER",
    "EXEMPT_FROM",
    "INTERACTS_WITH",
]


class Entity(BaseModel):
    type: EntityType
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)


class Relation(BaseModel):
    type: RelationType
    head: str
    tail: str
    source_chunk_id: str
    confidence: float = Field(ge=0.0, le=1.0)


class Extraction(BaseModel):
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relation] = Field(default_factory=list)


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
