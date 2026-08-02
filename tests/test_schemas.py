"""Smoke tests for the ontology-constrained schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.ingest.extract import ALLOWED_ENDPOINTS, endpoint_violations, orphan_entities
from src.schemas import Chunk, ContextDoc, Entity, Extraction, Relationship


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


# --------------------------------------------------------------------------
# Ontology v3: DefinedTerm / Right / Penalty + GRANTS / SETS_PENALTY
# --------------------------------------------------------------------------


@pytest.mark.parametrize("entity_type", ["DefinedTerm", "Right", "Penalty"])
def test_entity_accepts_v3_types(entity_type):
    assert Entity(type=entity_type, canonical_name="x").type == entity_type


@pytest.mark.parametrize("relation_type", ["GRANTS", "SETS_PENALTY"])
def test_relationship_accepts_v3_types(relation_type):
    r = Relationship(
        type=relation_type, head="a", tail="b", source_chunk_id="x", confidence=0.9
    )
    assert r.type == relation_type


def test_every_relation_type_has_an_endpoint_rule():
    """A relation with no entry in ALLOWED_ENDPOINTS is silently unchecked --
    exactly the gap that let ENFORCED_BY point at a Regulation."""
    from typing import get_args

    from src.ingest.extract import RelationType

    assert set(get_args(RelationType)) == set(ALLOWED_ENDPOINTS)


# --------------------------------------------------------------------------
# Post-parse integrity checks. Pydantic validates the type *string* only, so
# these cover the semantic errors it structurally cannot see.
# --------------------------------------------------------------------------


def _extraction(entities, relationships):
    return Extraction(
        chunk_id="c1",
        entities=[Entity(type=t, canonical_name=n) for t, n in entities],
        relationships=[
            Relationship(type=t, head=h, tail=tl, source_chunk_id="c1", confidence=0.9)
            for t, h, tl in relationships
        ],
    )


def test_endpoint_violation_caught_for_enforced_by_regulation():
    """The real bug from the pre-flight probe: ENFORCED_BY pointed at a
    Regulation instead of an Authority, and passed validation clean."""
    x = _extraction(
        [("Obligation", "comply"), ("Regulation", "AIA")],
        [("ENFORCED_BY", "comply", "AIA")],
    )
    assert x.relationships  # schema-valid ...
    violations = endpoint_violations(x)
    assert len(violations) == 1 and "tail=Regulation" in violations[0]


def test_endpoint_violation_caught_for_article_headed_exempt_from():
    """'other than those laid down in Article 5' produced
    EXEMPT_FROM: AIA Art. 5 -> AIA Art. 99, asserting a false exemption."""
    x = _extraction(
        [("Article", "AIA Art. 5"), ("Article", "AIA Art. 99(4)")],
        [("EXEMPT_FROM", "AIA Art. 5", "AIA Art. 99(4)")],
    )
    assert "head=Article" in endpoint_violations(x)[0]


def test_valid_penalty_chain_has_no_violations():
    x = _extraction(
        [
            ("Article", "AIA Art. 99(4)"),
            ("Obligation", "comply with provider obligations"),
            ("Penalty", "administrative fine up to EUR 15 000 000 or 3 %"),
        ],
        [
            ("SETS_PENALTY", "AIA Art. 99(4)", "administrative fine up to EUR 15 000 000 or 3 %"),
            ("PENALIZED_UNDER", "comply with provider obligations", "AIA Art. 99(4)"),
        ],
    )
    assert endpoint_violations(x) == []


def test_orphan_entities_found():
    """dangling_refs looks for edges with no entity; this is the mirror image.
    Nine cited Articles were declared and left unconnected in aia-art99-para4."""
    x = _extraction(
        [("Article", "AIA Art. 99(4)"), ("Article", "AIA Art. 16"), ("Obligation", "comply")],
        [("IMPOSES", "AIA Art. 99(4)", "comply")],
    )
    assert orphan_entities(x) == ["AIA Art. 16"]


# --------------------------------------------------------------------------
# ContextDoc (ADR-0011) -- a query/answer document is not a corpus row
# --------------------------------------------------------------------------


def test_context_doc_passage_carries_a_score():
    d = ContextDoc(
        chunk_id="aia-art9-para1", text="...", citation_label="AIA Art. 9(1)",
        source="PASSAGE", score=0.83,
    )
    assert d.source == "PASSAGE"
    assert d.score == 0.83
    assert d.derived is False


def test_context_doc_graph_statement_needs_no_score_but_keeps_provenance():
    """A rendered graph statement has no similarity score, but it must still
    name the chunk(s) that asserted it -- provenance is what makes it citable."""
    d = ContextDoc(
        chunk_id="aia-art26-para9", text="...", citation_label="AIA Art. 26(9)",
        source="GRAPH", derived=True,
    )
    assert d.score is None
    assert d.derived is True
    assert d.chunk_id and d.citation_label


def test_context_doc_rejects_unknown_source():
    with pytest.raises(ValidationError):
        ContextDoc(chunk_id="x", text="...", citation_label="x", source="WEB")


def test_context_doc_is_not_chunk():
    """Chunk stays extra='forbid' and unaware of ContextDoc's fields -- the two
    describe different things and a corpus row must not silently grow a score."""
    with pytest.raises(ValidationError):
        Chunk(
            chunk_id="aia-art9-para1", regulation="AIA", text="...",
            article=9, paragraph=1, score=0.83,
        )
