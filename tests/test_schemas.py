"""Smoke tests for the ontology-constrained schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas import Entity, Extraction, Relationship


def test_entity_valid_type():
    e = Entity(type="ActorRole", canonical_name="deployer", aliases=["deployers"])
    assert e.canonical_name == "deployer"


def test_entity_rejects_unknown_type():
    with pytest.raises(ValidationError):
        Entity(type="RandomThing", canonical_name="x")


def test_entity_accepts_lawful_basis():
    """Ontology v2: LawfulBasis is the 9th entity type."""
    e = Entity(type="LawfulBasis", canonical_name="consent of the data subject")
    assert e.type == "LawfulBasis"


def test_relationship_confidence_bounds():
    with pytest.raises(ValidationError):
        Relationship(
            type="IMPOSES",
            head="AIA Art. 26",
            tail="maintain documentation",
            source_chunk_id="aia-art26-para1",
            confidence=1.5,
        )


def test_relationship_accepts_permits():
    """Ontology v2: PERMITS is the 11th relationship type."""
    r = Relationship(
        type="PERMITS",
        head="GDPR Art. 6(1)",
        tail="consent of the data subject",
        source_chunk_id="gdpr-art6-para1",
        confidence=0.95,
    )
    assert r.type == "PERMITS"


def test_relationship_rejects_unknown_type():
    with pytest.raises(ValidationError):
        Relationship(
            type="ALLOWS", head="a", tail="b", source_chunk_id="x", confidence=0.5
        )


def test_extraction_requires_chunk_id():
    """chunk_id carries provenance, so it is mandatory."""
    with pytest.raises(ValidationError):
        Extraction(entities=[], relationships=[])


def test_extraction_round_trips():
    x = Extraction(chunk_id="gdpr-art6-para1", entities=[], relationships=[])
    assert x.chunk_id == "gdpr-art6-para1"
