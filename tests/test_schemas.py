"""Smoke tests for the ontology-constrained schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas import Entity, Extraction, Relation


def test_entity_valid_type():
    e = Entity(type="ActorRole", canonical_name="deployer", aliases=["deployers"])
    assert e.canonical_name == "deployer"


def test_entity_rejects_unknown_type():
    with pytest.raises(ValidationError):
        Entity(type="RandomThing", canonical_name="x")


def test_relation_confidence_bounds():
    with pytest.raises(ValidationError):
        Relation(
            type="IMPOSES",
            head="AIA Art. 26",
            tail="maintain documentation",
            source_chunk_id="aia-art26-para1",
            confidence=1.5,
        )


def test_extraction_defaults_empty():
    x = Extraction()
    assert x.entities == []
    assert x.relationships == []
