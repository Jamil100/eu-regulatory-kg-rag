"""Graph loader tests.

Most of these need no database, because `build_graph()` is pure -- the counts the
loader reports are derived, so they are assertable without Neo4j running. The rest
exercise the properties that only exist once loaded (idempotency, the templates)
and skip when Bolt is unreachable.

The idempotency check is here rather than in a runbook on purpose: "re-running
ingestion is a no-op" is the roadmap's stated done-criterion for this phase, and
`docs/failure-notes.md` already records that verifying something once by hand is
not the same as it being true.
"""

from __future__ import annotations

import collections
import re
import unicodedata
from typing import get_args

import pytest

from src.ingest import entity_resolution as er
from src.ingest import graph_writer
from src.ingest.extract import ALLOWED_ENDPOINTS, EntityType, RelationType
from src.query.cypher_templates import TEMPLATES

# Measured on the full corpus under ontology v3 (1,107 chunks). These are
# regression anchors: if extraction or resolution changes, these numbers should
# change deliberately and visibly, not silently.
EXPECTED_NODES = 3_366
EXPECTED_EDGES = 6_680          # 6,658 extracted + 22 derived cross-regulation bridges
EXPECTED_DANGLING = 107
EXPECTED_VIOLATIONS = 230       # was 241; widening INTERACTS_WITH's head to Annex cleared 11
EXPECTED_ISOLATED = 112
EXPECTED_DERIVED = 22           # 19 Article->Article, 3 Annex->Article


# --------------------------------------------------------------------------
# Pure derivation -- no database
# --------------------------------------------------------------------------

def test_graph_shape(graph):
    stats = graph["stats"]
    assert stats["nodes"] == EXPECTED_NODES
    assert stats["edges"] == EXPECTED_EDGES
    assert stats["skipped_dangling"] == EXPECTED_DANGLING
    assert stats["endpoint_violations"] == EXPECTED_VIOLATIONS
    assert stats["isolated_nodes"] == EXPECTED_ISOLATED


def test_every_label_and_relationship_type_is_in_the_ontology(graph):
    """Labels and relationship types are interpolated into Cypher, so a value
    from outside the closed Literal would be both a schema break and an
    injection surface."""
    assert {n["type"] for n in graph["nodes"]} <= set(get_args(EntityType))
    assert {e["type"] for e in graph["edges"]} <= set(get_args(RelationType))


def test_no_dangling_edge_survives_the_build(graph):
    """Edges are MATCHed against existing nodes at load time, so an endpoint with
    no node would silently drop the edge inside Neo4j instead of here."""
    names = {n["canonical_name"] for n in graph["nodes"]}
    for edge in graph["edges"]:
        assert edge["head"] in names
        assert edge["tail"] in names


def test_node_identity_is_unique_per_label(graph):
    """`apply_schema` puts a uniqueness constraint on every label, and type
    reconciliation is what makes that safe. If a name ever carried two types the
    constraint would reject the load."""
    ids = [(n["type"], n["canonical_name"]) for n in graph["nodes"]]
    assert len(ids) == len(set(ids))
    by_name = collections.Counter(n["canonical_name"] for n in graph["nodes"])
    assert [name for name, count in by_name.items() if count > 1] == []


def test_edges_are_keyed_by_source_chunk(graph):
    """Provenance is in the MERGE key, so (head, TYPE, tail, chunk) must be
    unique -- that is exactly what makes a second load a no-op."""
    keys = [
        (e["type"], e["head"], e["tail"], e["source_chunk_id"]) for e in graph["edges"]
    ]
    assert len(keys) == len(set(keys))


def test_violations_are_tagged_not_dropped(graph):
    """Policy: endpoint violations load with a flag. Recomputing the flag from
    ALLOWED_ENDPOINTS must agree with what the builder tagged."""
    types = {n["canonical_name"]: n["type"] for n in graph["nodes"]}
    tagged = recomputed = 0
    for edge in graph["edges"]:
        allowed = ALLOWED_ENDPOINTS.get(edge["type"])
        if not allowed:
            continue
        bad = (types[edge["head"]] not in allowed[0]
               or types[edge["tail"]] not in allowed[1])
        recomputed += bad
        tagged += edge["endpoint_violation"]
        assert edge["endpoint_violation"] == bad
    assert tagged == recomputed == EXPECTED_VIOLATIONS


def test_derived_bridges_are_article_level_and_tagged(graph):
    """The AI Act <-> GDPR bridge is specified article-to-article, but the extractor
    filed every one as REFERENCES and pointed INTERACTS_WITH at the instrument. The
    derived pass promotes them; `derived` keeps them honest about their origin."""
    derived = [e for e in graph["edges"] if e.get("derived")]
    assert len(derived) == EXPECTED_DERIVED
    assert all(e["type"] == "INTERACTS_WITH" for e in derived)

    types = {n["canonical_name"]: n["type"] for n in graph["nodes"]}
    shapes = collections.Counter((types[e["head"]], types[e["tail"]]) for e in derived)
    assert shapes == {("Article", "Article"): 19, ("Annex", "Article"): 3}


def test_derivation_never_invents_a_bridge_between_non_citing_provisions(graph):
    """AIA Art. 99 and GDPR Art. 83 are the two penalty regimes and are conceptually
    parallel, but neither cites the other -- so there is no REFERENCES edge to
    promote and no bridge may appear. The eval set marks those questions
    `graph_traversable: false` rather than being served a fabricated edge."""
    derived = [e for e in graph["edges"] if e.get("derived")]
    for edge in derived:
        ends = edge["head"] + edge["tail"]
        assert "aia art. 99" not in ends, edge
        assert "gdpr art. 83" not in ends, edge


def test_derived_bridges_keep_provenance(graph):
    """A derived edge still has to answer 'which paragraph asserted this', because
    source_chunk_id is the citation key and the join to pgvector."""
    for edge in (e for e in graph["edges"] if e.get("derived")):
        assert edge["source_chunk_id"]
        assert 0.0 <= edge["confidence"] <= 1.0


def test_both_endpoints_must_be_namespaced(graph):
    """Requiring only the head to carry an instrument prefix treated bare article
    names as foreign and invented 16 bridges. Both ends must be namespaced."""
    prefixes = ("aia", "gdpr", "led", "eudpr")
    for edge in (e for e in graph["edges"] if e.get("derived")):
        assert edge["head"].startswith(prefixes), edge
        assert edge["tail"].startswith(prefixes), edge


def test_isolated_nodes_are_kept(graph):
    """Policy: an entity with no edge still loads. Dropping them would erase the
    orphan-entity signal that `orphan_entities()` exists to surface."""
    assert graph["stats"]["isolated_nodes"] > 0
    connected = {e["head"] for e in graph["edges"]} | {e["tail"] for e in graph["edges"]}
    assert len(graph["nodes"]) - len(connected) == EXPECTED_ISOLATED


# --------------------------------------------------------------------------
# The normalize() paren fix (ADR-0009 correction)
# --------------------------------------------------------------------------

def _normalize_before_the_fix(name: str) -> str:
    """`normalize()` as it stood when it ended with `.strip(" .,;:()[]")`."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"[\"'`]", "", text)
    text = re.sub(r"[\s ]+", " ", text)
    text = re.sub(r"[-‐-―]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" .,;:()[]")
    return er.ABBREVIATIONS.get(text, text)


def test_canonical_names_have_balanced_brackets(graph):
    """The regression guard. Sub-numbered articles are ~a third of all nodes, and
    `AIA Art. 1(1)` used to resolve to `aia art. 1(1` -- a key the query side
    would have had to reproduce forever."""
    for node in graph["nodes"]:
        name = node["canonical_name"]
        assert name.count("(") == name.count(")"), name
        assert name.count("[") == name.count("]"), name


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AIA Art. 1(1)", "aia art. 1(1)"),
        ("GDPR Art. 6(1)(f)", "gdpr art. 6(1)(f)"),
        ("Regulation (EU) 2016/679", "regulation (eu) 2016/679"),
        # Balanced brackets are kept even when they wrap the whole name. The rule
        # is "peel only what has no partner", and nothing in the corpus is a
        # fully-wrapped name, so buying a special case for it is not worth the
        # risk to the 1,169 article names the rule exists to protect.
        ("(b)", "(b)"),
        # Unmatched, so peeled -- this is the case the old `.strip()` got right
        # and the fix must not regress.
        ("AIA Art. 1(1", "aia art. 1(1"),
        ("high-risk", "high risk"),
        # Short forms expand via ABBREVIATIONS; the full instrument names are
        # mapped earlier, by `normalize_instruments()` at parse time.
        ("gdpr", "GDPR"),
    ],
)
def test_normalize_keeps_matched_brackets_and_strips_unmatched(raw, expected):
    assert er.normalize(raw) == expected


def test_the_paren_fix_did_not_change_which_names_merge():
    """The fix changes node *keys*, so it has to be proved merge-neutral: same
    grouping of raw names, same count. Otherwise it is an unreviewed resolution
    change wearing a formatting change's clothes."""
    raw = {m["canonical_name"] for m in er.load_entities()}

    def group(norm) -> set[frozenset[str]]:
        mapped = {name: norm(name) for name in raw}
        plurals = er._plural_map(set(mapped.values()))
        key = {name: plurals.get(mapped[name], mapped[name]) for name in raw}
        buckets = collections.defaultdict(set)
        for name in raw:
            buckets[key[name]].add(name)
        return {frozenset(v) for v in buckets.values()}

    assert group(er.normalize) == group(_normalize_before_the_fix)


def test_display_name_is_a_readable_surface_form(graph):
    by_name = {n["canonical_name"]: n for n in graph["nodes"]}
    assert by_name["aia art. 1(1)"]["display_name"] == "AIA Art. 1(1)"
    assert by_name["high risk"]["display_name"] == "high-risk"
    for node in graph["nodes"]:
        assert node["display_name"], node["canonical_name"]


# --------------------------------------------------------------------------
# Against a live database
# --------------------------------------------------------------------------

def test_schema_is_rerunnable(driver):
    """All DDL is IF NOT EXISTS, so applying it twice must not raise."""
    graph_writer.apply_schema(driver)
    graph_writer.apply_schema(driver)


def test_loading_twice_is_a_no_op(loaded, graph):
    """The Step 4 criterion. Re-merge everything and assert nothing moved."""
    before = graph_writer.graph_counts(loaded)
    graph_writer.write_nodes(loaded, graph["nodes"])
    graph_writer.write_edges(loaded, graph["edges"])
    assert graph_writer.graph_counts(loaded) == before


def test_database_matches_the_derivation(loaded, graph):
    counts = graph_writer.graph_counts(loaded)
    stats = graph["stats"]
    assert counts["nodes"] == stats["nodes"]
    assert counts["edges"] == stats["edges"]
    assert counts["endpoint_violations"] == stats["endpoint_violations"]
    assert counts["isolated_nodes"] == stats["isolated_nodes"]
    assert counts["nodes_by_type"] == stats["nodes_by_type"]
    assert counts["edges_by_type"] == stats["edges_by_type"]


# Parameters are drawn from the loaded graph, not invented -- an ontology change
# that empties one of these should fail the test rather than the parameter.
TEMPLATE_CASES = [
    ("obligations_for_role", {"role": "deployer"}),
    ("obligations_for_system", {"system_type": "high risk ai system"}),
    ("definition_of", {"term": "provider"}),
    # Defined in AIA Annex IV, not an Article. `definition_of` pinned :Article on
    # the tail and missed 48 such terms; `provider` above could not catch it.
    ("definition_of", {"term": "computational resources"}),
    ("cross_regulation", {"article": "aia art. 2(7)"}),
    ("path_between", {"entity_a": "deployer", "entity_b": "GDPR"}),
]


@pytest.mark.parametrize("name,params", TEMPLATE_CASES)
def test_template_returns_rows(loaded, name, params):
    """A template returning zero rows means the graph shape does not match what
    Phase 3 assumes. `cross_regulation` was exactly that: it required
    Article<->Article and every INTERACTS_WITH edge points at a Regulation."""
    with loaded.session() as session:
        assert list(session.run(TEMPLATES[name], **params)), f"{name} returned no rows"


def test_enforcement_chain_returns_rows(loaded):
    """Kept separate because its parameter has to be discovered: only 216
    obligations carry ENFORCED_BY and only 4 of those also carry PENALIZED_UNDER."""
    with loaded.session() as session:
        obligation = session.run(
            "MATCH (o:Obligation)-[:ENFORCED_BY]->(:Authority) "
            "RETURN o.canonical_name AS name LIMIT 1"
        ).single()["name"]
        assert list(session.run(TEMPLATES["enforcement_chain"], obligation=obligation))


def test_definition_of_reaches_annex_defined_terms(loaded):
    """Every term with a DEFINED_IN edge must be reachable by the template. The
    endpoint contract allows Article *or* Annex, so pinning either one drops terms
    -- 48 of 337 when it was pinned to :Article."""
    with loaded.session() as session:
        defined = session.run(
            "MATCH (t:Entity)-[:DEFINED_IN]->(:Article|Annex) RETURN count(DISTINCT t) AS n"
        ).single()["n"]
        annex_only = session.run(
            "MATCH (t:Entity)-[:DEFINED_IN]->(:Annex) "
            "WHERE NOT EXISTS { MATCH (t)-[:DEFINED_IN]->(:Article) } "
            "RETURN count(DISTINCT t) AS n"
        ).single()["n"]
    assert defined == 334
    # These are the ones an :Article-only tail dropped on the floor.
    assert annex_only == 48


def test_templates_are_distinct_projections(loaded):
    """Edges are stored per asserting chunk, so parallel edges multiply rows --
    `high risk ai system -[:CLASSIFIED_AS]-> high risk` is asserted by 124 chunks
    and inflated this template to 24,428 rows before DISTINCT."""
    with loaded.session() as session:
        rows = list(session.run(
            TEMPLATES["obligations_for_system"], system_type="high risk ai system"
        ))
    assert len(rows) == 169


def test_chunk_ids_are_present_for_the_pgvector_join(loaded):
    """chunk_id is the shared key between Neo4j and pgvector. A node without one
    is unreachable from the vector side."""
    with loaded.session() as session:
        missing = session.run(
            "MATCH (n:Entity) WHERE n.chunk_ids IS NULL OR size(n.chunk_ids) = 0 "
            "RETURN count(n) AS n"
        ).single()["n"]
    assert missing == 0
