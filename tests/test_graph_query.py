"""The template library's parameter contract, and what it returns from a live graph.

The first tests for `src/query/`, which had none. The live half exists because
this project's record on Cypher written against a graph nobody queried is three
defects in six templates -- and all three returned rows. "Returns rows" is a
smoke test, so what is asserted here is row *counts*, provenance *coverage*, and
the one thing a wrong aggregation would break silently.
"""

from __future__ import annotations

import re

import pytest

from src.ingest import graph_writer
from src.query.cypher_templates import TEMPLATE_PARAMS, TEMPLATES
from src.query.graph_query import (
    BASELINE_CASES,
    provenance_of,
    row_to_dict,
    run_template,
    validate,
)

# ADR-0010 derived exactly 22 bridges, all INTERACTS_WITH.
EXPECTED_DERIVED = 22

# The row count `obligations_for_system` returns when the relationship is in the
# grouping key instead of inside collect(). Measured, not estimated.
NAIVE_MULTIPLIED_ROWS = 24_428


# --------------------------------------------------------------------------
# The parameter contract -- pure, no database
# --------------------------------------------------------------------------

def test_every_template_declares_its_parameters():
    assert set(TEMPLATES) == set(TEMPLATE_PARAMS)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_declared_parameters_match_the_cypher(name):
    """The two dicts are separate, so nothing but this test stops them drifting.

    A parameter declared but not used would pass validation and then do nothing;
    one used but not declared would be rejected as 'unexpected' by validate()
    even though the query needs it.
    """
    in_cypher = set(re.findall(r"\$(\w+)", TEMPLATES[name]))
    assert in_cypher == set(TEMPLATE_PARAMS[name])


def test_validate_rejects_an_unknown_template():
    with pytest.raises(ValueError, match="is not a template"):
        validate("drop_everything", {})


def test_validate_rejects_a_missing_parameter():
    with pytest.raises(ValueError, match="missing=\\['entity_b'\\]"):
        validate("path_between", {"entity_a": "deployer"})


def test_validate_rejects_an_undeclared_parameter():
    """The driver ignores an extra parameter, so without this the caller gets a
    query with an unfilled $role rather than an error naming the mistake."""
    with pytest.raises(ValueError, match="unexpected=\\['system_type'\\]"):
        validate("obligations_for_role", {"role": "deployer", "system_type": "x"})


def test_run_template_validates_before_it_opens_a_driver():
    """ADR-0002's control has to fire on the model's output, not after a
    connection succeeds -- with no driver passed this would raise
    ServiceUnavailable if validation ran second."""
    with pytest.raises(ValueError):
        run_template("'; MATCH (n) DETACH DELETE n //", {}, driver=None)


def test_row_to_dict_passes_plain_values_through():
    assert row_to_dict({"n": 1, "chunks": ["a", "b"], "missing": None}) == {
        "n": 1, "chunks": ["a", "b"], "missing": None,
    }


def test_provenance_of_reads_every_template_shape():
    """Callers ask 'which chunks assert this row', not 'what did this template
    name its provenance column'."""
    assert provenance_of({"applies_chunks": ["a"], "imposes_chunks": ["b"]}) == ["a", "b"]
    assert provenance_of({"chunks": ["a", "b"]}) == ["a", "b"]
    assert provenance_of(
        {"provenance": [{"chunk": "a", "derived": True}, {"chunk": "b", "derived": False}]}
    ) == ["a", "b"]


def test_provenance_of_dedupes_and_drops_nulls():
    """An empty OPTIONAL leg contributes nothing, and the same chunk asserting
    two legs is one citation, not two."""
    assert provenance_of({"a_chunks": ["x", "x"], "b_chunks": [], "c_chunks": ["x"]}) == ["x"]
    assert provenance_of({"provenance": [{"chunk": None}]}) == []
    assert provenance_of({"score": 0.4, "label": "AIA Art. 9(2)"}) == []


# --------------------------------------------------------------------------
# Against a live database
# --------------------------------------------------------------------------

def _discover_enforced_obligation(driver, penalized: bool = True) -> str:
    """Only 216 of 1,179 obligations carry ENFORCED_BY and only 4 of those also
    carry PENALIZED_UNDER, so both parameters have to come from the graph."""
    clause = "" if penalized else "NOT "
    with driver.session() as session:
        row = session.run(
            "MATCH (o:Obligation)-[:ENFORCED_BY]->(:Authority) "
            f"WHERE {clause}EXISTS {{ MATCH (o)-[:PENALIZED_UNDER]->(:Article) }} "
            "RETURN o.canonical_name AS name ORDER BY name LIMIT 1"
        ).single()
    assert row is not None, "no obligation matched; the graph shape changed"
    return row["name"]


@pytest.mark.parametrize("name,params,expected", BASELINE_CASES)
def test_baseline_row_counts_are_unchanged(loaded, name, params, expected):
    """The regression anchors from docs/metrics/graph-load.md.

    Only the 169 was ever asserted in code; the other five lived in a document,
    where nothing enforced them. Projecting provenance is exactly the change that
    could move them, so they are all asserted here now.
    """
    assert len(run_template(name, params, loaded)) == expected


def test_enforcement_chain_row_count(loaded):
    obligation = _discover_enforced_obligation(loaded)
    assert len(run_template("enforcement_chain", {"obligation": obligation}, loaded)) >= 1


def test_aggregating_provenance_is_what_holds_the_row_count(loaded):
    """The reason behind the 169, not just the number.

    Naming the relationship and returning `rel.source_chunk_id` as a column puts
    it in the grouping key, and the 124 chunks asserting `high risk ai system
    -[:CLASSIFIED_AS]-> high risk` multiply the result. Inside collect() the same
    data comes back on 169 rows. If someone 'simplifies' a template back to a
    projected column, this fails with both numbers in the message.
    """
    naive = """
        MATCH (s:SystemType {canonical_name: $system_type})-[ca:CLASSIFIED_AS]->(rc:RiskCategory)
        OPTIONAL MATCH (s)<-[ap:APPLIES_TO]-(o:Obligation)<-[im:IMPOSES]-(a)
        RETURN DISTINCT s, rc, o, a, ca.source_chunk_id, ap.source_chunk_id, im.source_chunk_id
    """
    with loaded.session() as session:
        multiplied = len(list(session.run(naive, system_type="high risk ai system")))
    aggregated = len(
        run_template("obligations_for_system", {"system_type": "high risk ai system"}, loaded)
    )
    assert multiplied == NAIVE_MULTIPLIED_ROWS
    assert aggregated == 169


@pytest.mark.parametrize("name,params,expected", BASELINE_CASES)
def test_every_row_is_citable(loaded, name, params, expected):
    """A row with no source_chunk_id cannot be cited or validated, which is the
    whole defect this step exists to close."""
    for row in run_template(name, params, loaded):
        assert provenance_of(row), f"{name} returned a row with no provenance: {row}"


def test_an_empty_optional_leg_collects_to_nothing(loaded):
    """`collect` drops nulls, so a missed OPTIONAL MATCH yields [] -- but only
    because the template collects the property. A map literal is never null even
    when all its values are, and `[{chunk: null}]` is a fake citation with a null
    chunk id that citation validation would have to catch instead."""
    obligation = _discover_enforced_obligation(loaded, penalized=False)
    rows = run_template("enforcement_chain", {"obligation": obligation}, loaded)
    assert rows
    for row in rows:
        assert row["penalty_chunks"] == []
        assert row["enforced_chunks"]


def test_provenance_chunk_ids_are_real(loaded):
    """source_chunk_id is the join key to pgvector. An id that names no chunk
    would be a citation pointing at nothing."""
    with loaded.session() as session:
        known = {
            chunk_id
            for row in session.run("MATCH (n:Entity) RETURN n.chunk_ids AS ids")
            for chunk_id in (row["ids"] or [])
        }
    for name, params, _ in BASELINE_CASES:
        for row in run_template(name, params, loaded):
            unknown = set(provenance_of(row)) - known
            assert not unknown, f"{name} cited chunks absent from the graph: {unknown}"


def test_derived_is_confined_to_interacts_with(loaded):
    """This is what licenses surfacing `derived` on only two of the six
    templates. ADR-0010 promotes cross-boundary REFERENCES to INTERACTS_WITH and
    nothing else, so the other four templates cannot traverse a derived edge. A
    new derivation rule that tags another type must fail here first."""
    with loaded.session() as session:
        by_type = {
            row["type"]: row["n"]
            for row in session.run(
                "MATCH ()-[r]->() WHERE r.derived RETURN type(r) AS type, count(*) AS n"
            )
        }
    assert by_type == {"INTERACTS_WITH": EXPECTED_DERIVED}


def test_a_derived_bridge_is_distinguishable_in_the_output(loaded):
    """ADR-0010 tagged the bridges so a consumer could tell an inferred edge from
    an asserted one. Phase 3 is that consumer, and this is the assertion that the
    flag survives the trip out of the graph."""
    with loaded.session() as session:
        head = session.run(
            "MATCH (a)-[r:INTERACTS_WITH]->(b) WHERE r.derived "
            "RETURN a.canonical_name AS name ORDER BY name LIMIT 1"
        ).single()["name"]

    rows = run_template("cross_regulation", {"article": head}, loaded)
    flags = [entry["derived"] for row in rows for entry in row["provenance"]]
    assert any(flags), f"no derived edge surfaced for {head!r}"
    for row in rows:
        for entry in row["provenance"]:
            assert isinstance(entry["derived"], bool)
            assert isinstance(entry["outbound"], bool)


def test_path_between_carries_one_flag_per_hop(loaded):
    """The path's provenance is positional -- chunks[i], types[i] and
    derived_flags[i] describe the same hop -- so a length mismatch would silently
    attribute a chunk to the wrong relationship."""
    rows = run_template("path_between", {"entity_a": "deployer", "entity_b": "GDPR"}, loaded)
    assert rows
    for row in rows:
        hops = row["p"]["hops"]
        assert len(row["chunks"]) == hops
        assert len(row["types"]) == hops
        assert len(row["derived_flags"]) == hops


def test_projected_nodes_carry_display_name_for_prose(loaded):
    """Step 5 renders prose from these rows and must use display_name, or the
    graph path cites `aia art. 1(1)` and `high risk` instead of `AIA Art. 1(1)`
    and `high-risk` (ADR-0009 Correction)."""
    for name, params, _ in BASELINE_CASES:
        for row in run_template(name, params, loaded):
            for value in row.values():
                if isinstance(value, dict) and "labels" in value:
                    assert value.get("display_name"), f"{name}: {value.get('canonical_name')}"


def test_the_templates_still_run_as_plain_strings(loaded):
    """`TEMPLATES[name]` stays a string a caller can hand straight to
    `session.run` -- test_graph_writer.py does exactly that, and the parameter
    contract was added as a sibling dict so it would keep working."""
    with loaded.session() as session:
        assert len(list(session.run(TEMPLATES["obligations_for_role"], role="deployer"))) == 60


def test_graph_counts_are_the_ones_these_anchors_were_measured_against(loaded):
    """If the graph moved, a row count that moved with it is not a template
    defect -- this keeps the two diagnoses apart."""
    counts = graph_writer.graph_counts(loaded)
    assert counts["nodes"] == 3_366
    assert counts["edges"] == 6_680
