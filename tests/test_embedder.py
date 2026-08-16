"""The vector index: the pure parts always, the loaded table when it is there.

Split the way `test_graph_writer.py` is split -- anything derivable without a
database is asserted unconditionally, and the DB-backed checks skip rather than
redden when Postgres is not running.

No test here calls the Cohere API. Embedding is paid and non-deterministic to
schedule; what is worth testing is the schema, the load and the join, all of
which are already on disk once `--apply` has run.
"""

from __future__ import annotations

import pytest

from src.index.embedder import DIMENSIONS, load_corpus
from src.index.recall_harness import load_labeled_queries

EXPECTED_TOTAL = 1108
EXPECTED_SHAPES = {"paragraph": 906, "annex": 108, "definition": 94}

# Eval rows carrying at least one gold chunk, i.e. everything the recall harness
# can score. 21 at the 23-row eval set; 90 at the 100-row set (2026-08-15).
LABELED_QUERIES = 90


# --------------------------------------------------------------------------
# Pure -- no database
# --------------------------------------------------------------------------

def test_load_corpus_is_the_whole_corpus():
    chunks = load_corpus()
    assert len(chunks) == EXPECTED_TOTAL
    counts: dict[str, int] = {}
    for chunk in chunks:
        counts[chunk.shape] = counts.get(chunk.shape, 0) + 1
    assert counts == EXPECTED_SHAPES


def test_labeled_queries_all_have_gold_chunks():
    """The harness must never score a row that has nothing to be scored against.

    The `out-of-scope` and `unanswerable` rows correctly carry no gold chunks,
    and counting them as recall misses would penalise the system for behaving
    correctly. `hard-negative` is the refusal mode that DOES carry gold, and it
    is deliberately in scope here.

    21 at the 23-row set, 90 at the 100-row set (2026-08-15): 100 questions minus
    the 5 out-of-scope and 5 unanswerable rows. The count is pinned because a
    silent drop would shrink every recall denominator without changing a rate.
    """
    queries = load_labeled_queries()
    assert len(queries) == LABELED_QUERIES
    assert all(q["source_chunk_ids"] for q in queries)
    assert {q["stratum"] for q in queries}.isdisjoint({"out-of-scope", "unanswerable"})


def test_gold_chunk_ids_exist_in_the_corpus():
    """A typo in a gold id would show up as a permanent, unexplained recall miss."""
    corpus = {c.chunk_id for c in load_corpus()}
    referenced = {
        chunk_id
        for query in load_labeled_queries()
        for chunk_id in query["source_chunk_ids"]
    }
    assert referenced <= corpus, f"gold ids not in the corpus: {sorted(referenced - corpus)}"


# --------------------------------------------------------------------------
# Loaded table
# --------------------------------------------------------------------------

def test_every_chunk_is_indexed(indexed):
    assert indexed.execute("SELECT count(*) FROM chunks").fetchone()[0] == EXPECTED_TOTAL
    by_shape = dict(
        indexed.execute("SELECT shape, count(*) FROM chunks GROUP BY shape").fetchall()
    )
    assert by_shape == EXPECTED_SHAPES


def test_both_dimension_arms_cover_the_same_rows(indexed):
    """ADR-0004 only means anything if 1536 and 512 see an identical row set."""
    for column in DIMENSIONS.values():
        missing = indexed.execute(
            f"SELECT count(*) FROM chunks WHERE {column} IS NULL"
        ).fetchone()[0]
        assert missing == 0, f"{column} has {missing} unembedded rows"


def test_provenance_survives_the_load(indexed):
    """Annex and definition chunks keep the fields that identify them.

    The previous schema had `article` and `paragraph` only, which would have
    loaded 202 chunks as anonymous text -- Annex III is the high-risk list.
    """
    assert indexed.execute("SELECT count(*) FROM chunks WHERE citation_label IS NULL").fetchone()[0] == 0
    assert indexed.execute(
        "SELECT count(*) FROM chunks WHERE shape='annex' AND (annex IS NULL OR point IS NULL)"
    ).fetchone()[0] == 0
    assert indexed.execute(
        "SELECT count(*) FROM chunks WHERE shape='definition' AND definition IS NULL"
    ).fetchone()[0] == 0
    assert indexed.execute(
        "SELECT count(*) FROM chunks WHERE section IS NOT NULL"
    ).fetchone()[0] == 32


def test_the_unextracted_chunk_is_still_reachable(indexed):
    """`gdpr-art70-para1` never extracted, so the graph has nothing for it.

    Its presence here is the whole claim that the vector path covers the graph's
    gap, and it is also why it is the only chunk with empty `entity_ids`.
    """
    row = indexed.execute(
        "SELECT citation_label, cardinality(entity_ids) FROM chunks "
        "WHERE chunk_id = 'gdpr-art70-para1'"
    ).fetchone()
    assert row == ("GDPR Art. 70(1)", 0)

    empty = [
        r[0] for r in indexed.execute(
            "SELECT chunk_id FROM chunks WHERE cardinality(entity_ids) = 0"
        ).fetchall()
    ]
    assert empty == ["gdpr-art70-para1"]


def test_entity_ids_name_real_graph_nodes(indexed, graph):
    """The pgvector->Neo4j join key, checked rather than assumed.

    A value in `entity_ids` must be usable directly as a Cypher parameter, which
    means it has to be the same string the loader MERGEd on.
    """
    names = {node["canonical_name"] for node in graph["nodes"]}
    dangling = set()
    for (ids,) in indexed.execute("SELECT entity_ids FROM chunks").fetchall():
        dangling |= {entity_id for entity_id in ids if entity_id not in names}
    assert not dangling, f"entity_ids not present as graph nodes: {sorted(dangling)[:10]}"


def test_citation_labels_are_unique_in_the_database(indexed):
    total, distinct = indexed.execute(
        "SELECT count(*), count(DISTINCT citation_label) FROM chunks"
    ).fetchone()
    assert total == distinct


@pytest.mark.parametrize("dim,column", sorted(DIMENSIONS.items()))
def test_search_returns_k_rows(indexed, dim, column):
    """A smoke test of the operator itself, using a stored vector as the probe.

    Reuses a row's own embedding rather than calling the API, so this costs
    nothing and is deterministic. A chunk must be its own nearest neighbour.
    """
    probe = indexed.execute(
        f"SELECT {column} FROM chunks WHERE chunk_id = 'aia-art9-para1'"
    ).fetchone()[0]
    rows = indexed.execute(
        f"SELECT chunk_id FROM chunks ORDER BY {column} <=> %s::vector LIMIT 10",
        (probe,),
    ).fetchall()
    assert len(rows) == 10
    assert rows[0][0] == "aia-art9-para1"
