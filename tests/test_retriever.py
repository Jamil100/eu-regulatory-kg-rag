"""What the vector retrieval path must not get wrong silently.

Almost every defect this file pins returns rows. Querying the 1536 column works;
sending `search_document` instead of `search_query` returns a valid vector;
ordering on the `similarity` alias returns the same rows at this corpus size;
leaving `enable_seqscan` set on a pooled connection changes nothing until Step 7.
None of those raise. That is why they are tested rather than reviewed.

Everything above the live-database banner runs with no container, no API key and
no spend.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.query.retriever import (
    DIM,
    RetrievalResult,
    RetrieverError,
    _column,
    embed_question,
    retrieve,
    retrieve_detailed,
    search_sql,
)
from src.schemas import ContextDoc


class FakeEmbedClient:
    """Mimics the slice of `cohere.ClientV2` that `_embed_call` touches."""

    def __init__(self, dim: int = DIM, tokens: int = 7) -> None:
        self.dim = dim
        self.tokens = tokens
        self.calls: list[dict] = []

    def embed(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            embeddings=SimpleNamespace(float_=[[0.1] * self.dim]),
            meta=SimpleNamespace(billed_units=SimpleNamespace(input_tokens=self.tokens)),
        )


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    """Records every statement executed, so a stray SET cannot hide."""

    def __init__(self, rows):
        self._rows = rows
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return FakeCursor(self._rows)

    def close(self):  # pragma: no cover - never called, conn is injected
        raise AssertionError("retrieve() closed a connection it did not own")


ROW = ("aia-art9-para1", "A risk management system shall be established.",
       "AIA Art. 9(1)", 0.64)


# --------------------------------------------------------------------------
# The dimension, which is a decision and not a default
# --------------------------------------------------------------------------

def test_retrieve_defaults_to_512_not_the_embedders_1536():
    """ADR-0004 is Accepted at 512 and `embed_query` defaults to 1536.

    Querying the wrong column raises nothing -- it works, and costs ~8x the
    latency the ADR was decided on. Only a test separates the two.
    """
    assert DIM == 512


def test_the_embedders_own_default_is_left_alone():
    """Changing a default changes every caller. The retriever is explicit instead."""
    import inspect

    from src.index.embedder import embed_query

    assert inspect.signature(embed_query).parameters["dim"].default == 1536


def test_an_unknown_dimension_is_refused_before_a_column_name_is_built():
    with pytest.raises(RetrieverError, match="no embedding column"):
        _column(768)


def test_the_two_real_dimensions_map_to_their_columns():
    assert _column(512) == "embedding_512"
    assert _column(1536) == "embedding_1536"


def test_a_vector_of_the_wrong_length_is_refused_before_the_query():
    with pytest.raises(RetrieverError, match="512 components|expected 512"):
        retrieve_detailed("q", 5, vector=[0.1] * 1536, conn=FakeConn([ROW]))


# --------------------------------------------------------------------------
# The SQL, whose defects all return rows
# --------------------------------------------------------------------------

def test_the_query_orders_on_the_distance_expression_not_the_similarity_alias():
    """An alias does not match the HNSW index expression and disables it.

    At 1,108 rows the planner picks a Seq Scan either way, so this is invisible
    today and only bites on a bigger corpus.
    """
    sql = search_sql("embedding_512")
    assert "ORDER BY embedding_512 <=> %(vec)s::vector ASC" in sql
    assert "ORDER BY similarity" not in sql


def test_the_query_casts_the_parameter_to_vector():
    """psycopg adapts a bare list to double precision[], which ORDER BY cannot use."""
    assert "%(vec)s::vector" in search_sql("embedding_512")


def test_the_query_breaks_distance_ties_on_chunk_id():
    """12 corpus texts are duplicated across 24 rows and draw equal distances.

    Without this the row at position k varies between runs and the committed
    artifact flakes at the k boundary.
    """
    assert search_sql("embedding_512").rstrip().endswith("LIMIT %(k)s")
    assert "ASC, chunk_id" in search_sql("embedding_512")


def test_the_query_selects_the_stored_citation_label():
    """`citation_label` is NOT NULL UNIQUE so the answer path reads it, never derives it."""
    assert "citation_label" in search_sql("embedding_512")


def test_retrieval_issues_no_session_settings():
    """`recall_harness` sets enable_seqscan and hnsw.ef_search; those are instruments.

    The connection is autocommit, so a plain SET persists, and at Step 7 the
    connection comes out of a pool -- the next request would inherit it.
    """
    conn = FakeConn([ROW])
    retrieve("q", 5, vector=[0.1] * DIM, conn=conn)
    assert not any("SET" in stmt.upper() for stmt in conn.statements)
    assert len(conn.statements) == 1


def test_an_empty_result_raises_rather_than_reporting_zero_recall():
    """`WHERE ... IS NOT NULL` turns a dropped column into [] rather than an error.

    vector-index.md §Open contemplates dropping `embedding_1536`. Returning []
    would read as a catastrophic retrieval finding instead of an unloaded store.
    """
    with pytest.raises(RetrieverError, match="empty or the table is unloaded"):
        retrieve("q", 5, vector=[0.1] * DIM, conn=FakeConn([]))


def test_top_k_below_one_is_refused():
    with pytest.raises(RetrieverError, match="at least 1"):
        retrieve("q", 0, vector=[0.1] * DIM, conn=FakeConn([ROW]))


def test_an_injected_connection_is_not_closed_by_the_retriever():
    """FakeConn.close() asserts. Step 7 owns the pool; this module owns nothing."""
    retrieve("q", 5, vector=[0.1] * DIM, conn=FakeConn([ROW]))


# --------------------------------------------------------------------------
# Embedding the question
# --------------------------------------------------------------------------

def test_the_retriever_asks_for_search_query_not_search_document():
    """Embed v4 is asymmetric and the wrong input_type raises nothing at all.

    It returns a perfectly valid vector that retrieves worse, so there is no
    error to catch and no symptom short of a recall regression.
    """
    client = FakeEmbedClient()
    embed_question("what is a high-risk system?", DIM, client)
    assert client.calls[0]["input_type"] == "search_query"


def test_the_retriever_asks_for_the_dimension_it_was_given():
    client = FakeEmbedClient()
    embed_question("q", DIM, client)
    assert client.calls[0]["output_dimension"] == 512


def test_embedding_returns_the_billed_tokens_for_the_cost_line():
    vector, tokens = embed_question("q", DIM, FakeEmbedClient(tokens=11))
    assert tokens == 11
    assert len(vector) == DIM


def test_retrieval_raises_rather_than_exiting_when_the_api_key_is_missing(monkeypatch):
    """`embedder.get_client()` raises SystemExit, which would kill a FastAPI worker.

    Catching SystemExit would mean catching BaseException, so the retriever owns
    a client factory instead -- the same split router.py made.
    """
    monkeypatch.setattr("src.config.settings.cohere_api_key", "")
    with pytest.raises(RetrieverError, match="is not set"):
        embed_question("q")


def test_an_api_error_becomes_a_retriever_error_not_a_system_exit():
    from cohere.core import ApiError

    class Failing(FakeEmbedClient):
        def embed(self, **kwargs):
            raise ApiError(status_code=400, body="bad request")

    with pytest.raises(RetrieverError, match="embedding the question failed"):
        embed_question("q", DIM, Failing())


# --------------------------------------------------------------------------
# The context document contract
# --------------------------------------------------------------------------

def test_context_doc_has_exactly_the_fields_two_adrs_put_there():
    """ContextDoc is not extra="forbid", so an eighth field can be bolted on.

    A separate `rerank_score` would give the object two score fields with no ADR
    and no rule about which one a consumer should trust. That is what this test
    was written to stop and it still is.

    `provenance` is the one widening that has cleared it. It was added by Step 6
    and recorded in ADR-0014 as a signature correction, the treatment ADR-0011
    and ADR-0013 used for the same kind of change, and it exists because
    `path_to_prose` was computing the list, rendering it into the statement text
    as labels, and then dropping it -- so ADR-0013's 24-of-32 was measured
    against a set `Citation` could never carry. Failing here is the correct
    behaviour for a field added without an ADR; this one has one.
    """
    assert set(ContextDoc.model_fields) == {
        "chunk_id", "text", "citation_label", "source", "score", "derived",
        "provenance",
    }


def test_a_retrieved_doc_is_labelled_as_a_passage_not_a_graph_statement():
    docs = retrieve("q", 5, vector=[0.1] * DIM, conn=FakeConn([ROW]))
    assert docs[0].source == "PASSAGE"
    assert docs[0].derived is False


def test_the_score_is_similarity_so_higher_is_better():
    """Rerank overwrites this field with a relevance score, also higher-is-better.

    The two are not comparable -- [-1,1] against [0,1] -- but neither ever means
    the opposite of the other, which is what makes a shared field survivable.
    """
    docs = retrieve("q", 5, vector=[0.1] * DIM, conn=FakeConn([ROW]))
    assert docs[0].score == pytest.approx(0.64)


def test_the_result_splits_embed_latency_from_search_latency():
    """vector-index.md quotes 6.68 ms p50 for SQL alone; a request pays both."""
    result = retrieve_detailed("q", 5, vector=[0.1] * DIM, conn=FakeConn([ROW]))
    assert isinstance(result, RetrievalResult)
    assert result.embed_ms == 0.0  # a vector was injected, so nothing was embedded
    assert result.latency_ms == result.embed_ms + result.search_ms


# --------------------------------------------------------------------------
# Against a live database -- still no API key, because vectors are injected
# --------------------------------------------------------------------------

def _stored_vector(conn, chunk_id: str) -> list[float]:
    """A chunk's own stored embedding, as a plain list.

    `pgvector_schema.connect()` calls `register_vector`, so the column comes back
    as a `Vector` rather than a list. Using a stored vector as the probe is what
    makes every database test below cost nothing and need no API key.
    """
    row = conn.execute(
        "SELECT embedding_512 FROM chunks WHERE chunk_id = %s", (chunk_id,)
    ).fetchone()
    return row[0].to_list()


def test_a_chunk_retrieves_itself_first_when_its_own_vector_is_the_query(indexed):
    vector = _stored_vector(indexed, "aia-art9-para1")
    docs = retrieve("unused", 5, vector=vector, conn=indexed)
    assert docs[0].chunk_id == "aia-art9-para1"
    assert docs[0].score == pytest.approx(1.0, abs=1e-6)


def test_retrieve_reads_the_512_column(indexed):
    """A 512-vector against `embedding_1536` raises a pgvector dimension mismatch.

    So a query that succeeds with a 512-length vector proves which column was
    read, for free and without trusting the column name in the SQL string.
    """
    vector = _stored_vector(indexed, "aia-art9-para1")
    assert len(vector) == 512
    assert retrieve("unused", 3, dim=512, vector=vector, conn=indexed)

    with pytest.raises(RetrieverError):
        retrieve("unused", 3, dim=1536, vector=vector, conn=indexed)


def test_scores_descend_so_the_first_result_is_the_closest(indexed):
    vector = _stored_vector(indexed, "aia-art26-para1")
    scores = [doc.score for doc in retrieve("unused", 20, vector=vector, conn=indexed)]
    assert scores == sorted(scores, reverse=True)


def test_the_top_50_prefix_at_k_equals_a_direct_query_at_that_k(indexed):
    """The whole k-matrix reports pre@5 and pre@10 as slices of one k=50 pass.

    That is only sound under exact search. Under forced HNSW pgvector raises
    ef_search to at least k and the prefixes diverge.
    """
    vector = _stored_vector(indexed, "aia-art26-para1")
    top50 = [d.chunk_id for d in retrieve("unused", 50, vector=vector, conn=indexed)]
    for k in (5, 10):
        direct = [d.chunk_id for d in retrieve("unused", k, vector=vector, conn=indexed)]
        assert direct == top50[:k]


def test_retrieve_and_the_recall_harness_return_the_same_ranking(indexed):
    """ADR-0004's numbers came out of `recall_harness.search()`.

    If this module ranked differently, every figure in vector-index.md would stop
    describing the code that now serves requests.
    """
    from src.index.recall_harness import search

    vector = _stored_vector(indexed, "gdpr-art83-para5")
    mine = [d.chunk_id for d in retrieve("unused", 10, vector=vector, conn=indexed)]
    theirs = search(indexed, vector, 512, 10, ef_search=0, force_index=False)
    assert mine == theirs


def test_every_doc_carries_the_stored_citation_label_not_a_recomputed_one(indexed):
    vector = _stored_vector(indexed, "aia-art9-para1")
    docs = retrieve("unused", 10, vector=vector, conn=indexed)
    stored = dict(
        indexed.execute(
            "SELECT chunk_id, citation_label FROM chunks WHERE chunk_id = ANY(%s)",
            ([d.chunk_id for d in docs],),
        ).fetchall()
    )
    assert {d.chunk_id: d.citation_label for d in docs} == stored


def test_retrieve_leaves_no_session_settings_behind(indexed):
    """The pooled-connection guard, against a real session rather than a fake."""
    before = indexed.execute("SHOW enable_seqscan").fetchone()[0]
    vector = _stored_vector(indexed, "aia-art9-para1")
    retrieve("unused", 10, vector=vector, conn=indexed)
    assert indexed.execute("SHOW enable_seqscan").fetchone()[0] == before


# --------------------------------------------------------------------------
# The lexical union pool (measured 2026-08-16, not shipped -- see
# answer_path.LEXICAL_DEPTH_LIVE)
# --------------------------------------------------------------------------

QUESTION = "Which GDPR fine tier covers a refusal to answer subject access requests?"


# THE VECTOR IS PINNED, AND THAT IS LOAD-BEARING RATHER THAN AN OPTIMISATION.
#
# These are A/B tests over the same draw, and `retrieve_detailed` embeds on every
# call. embed-v4.0 does not return byte-identical vectors for identical input:
# two live sweeps of the unchanged corpus differed in pool MEMBERSHIP on 7 of 100
# questions and in pool ORDER on 22 (`tests/test_reranker.py:PRE_5_UNSTABLE`).
# Written the obvious way, `test_lexical_depth_zero_is_exactly_the_old_behaviour`
# compares two independent embeddings and fails at the tail of the top-50 -- it
# did, at index 20, before this was pinned.
#
# Using a stored chunk embedding as the query vector fixes the vector half
# exactly, leaves the lexical half driven by the question text where it belongs,
# and costs no API call. Same injection point, and same reasoning, as
# `recall_at_k(vectors=...)`.
def _pool_args(conn):
    return {"conn": conn, "vector": _stored_vector(conn, "gdpr-art83-para5")}


def test_the_pool_is_a_superset_of_the_vector_draw(indexed):
    """Union, not replacement. The vector draw must survive intact and in order,
    because every number measured before the union existed was measured on it."""
    from src.query.retriever import retrieve_pool_detailed

    args = _pool_args(indexed)
    vector_only = retrieve_detailed(QUESTION, 50, **args)
    pooled = retrieve_pool_detailed(QUESTION, 50, lexical_depth=20, **args)

    vector_ids = [d.chunk_id for d in vector_only.docs]
    assert [d.chunk_id for d in pooled.docs[: len(vector_ids)]] == vector_ids
    assert len(pooled.docs) == len(vector_ids) + pooled.lexical_added


def test_lexical_depth_zero_is_exactly_the_old_behaviour(indexed):
    """The A/B control. If this drifts, the 'before' arm of every pool
    measurement stops being the system that was measured before."""
    from src.query.retriever import retrieve_pool_detailed

    args = _pool_args(indexed)
    baseline = retrieve_detailed(QUESTION, 50, **args)
    control = retrieve_pool_detailed(QUESTION, 50, lexical_depth=0, **args)

    assert [d.chunk_id for d in control.docs] == [d.chunk_id for d in baseline.docs]
    assert [d.score for d in control.docs] == [d.score for d in baseline.docs]
    assert control.lexical_added == 0
    assert control.lexical_ms == 0.0


def test_lexical_only_candidates_carry_no_score(indexed):
    """BM25 and ts_rank_cd are not on the cosine scale and must not pretend to be.

    A lexical-only candidate arriving with a plausible-looking float would be
    sortable against the vector half, and the resulting ordering would be
    meaningless in a way nothing downstream could detect.
    """
    from src.query.retriever import retrieve_pool_detailed

    pooled = retrieve_pool_detailed(
        QUESTION, 50, lexical_depth=20, **_pool_args(indexed)
    )
    assert pooled.lexical_added > 0, "no lexical candidates to check"

    scored = [d for d in pooled.docs if d.score is not None]
    unscored = [d for d in pooled.docs if d.score is None]
    assert len(scored) == 50, "the vector half must keep its cosine scores"
    assert len(unscored) == pooled.lexical_added
    assert all(d.source == "PASSAGE" for d in pooled.docs)


def test_the_pool_has_no_duplicates(indexed):
    """A chunk found by both the vector draw and a lexical arm must appear once.
    Sending it twice would pay to rerank it twice and let it take two of the five
    prompt slots."""
    from src.query.retriever import retrieve_pool_detailed

    pooled = retrieve_pool_detailed(
        QUESTION, 50, lexical_depth=50, **_pool_args(indexed)
    )
    ids = [d.chunk_id for d in pooled.docs]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# Enumeration: every limb of one provision, in statutory order
# --------------------------------------------------------------------------

def test_an_article_enumerates_in_statutory_order(indexed):
    """The ordering is the legislation's, not a measured one. That is the whole
    point: the measured orderings are what fail on this stratum."""
    from src.query.retriever import retrieve_by_article

    docs = retrieve_by_article("AIA", 26, conn=indexed)
    assert [d.chunk_id for d in docs] == [f"aia-art26-para{n}" for n in range(1, 13)]
    assert [d.citation_label for d in docs[:3]] == [
        "AIA Art. 26(1)", "AIA Art. 26(2)", "AIA Art. 26(3)"
    ]


def test_an_annex_enumerates_by_point(indexed):
    from src.query.retriever import retrieve_by_annex

    docs = retrieve_by_annex("AIA", "III", conn=indexed)
    assert [d.chunk_id for d in docs] == [f"aia-annex3-point{n}" for n in range(1, 9)]


def test_enumerated_docs_carry_no_score(indexed):
    """Statutory order is not a relevance scale. A float here would be sortable
    against cosine and rerank scores, which is the defect `retriever`'s module
    docstring forbids."""
    from src.query.retriever import retrieve_by_article

    assert all(d.score is None for d in retrieve_by_article("AIA", 99, conn=indexed))


def test_an_article_larger_than_the_bound_is_refused_whole(indexed):
    """`aia-art3` is the definitions article at 68 paragraphs.

    Refused rather than truncated: half of Article 3 is not "the definitions", it
    is an arbitrary prefix that reads as complete, and an answer built on it
    would be confidently partial. Returning [] lets the caller fall back to
    ranking, which is a correct outcome.
    """
    from src.query.retriever import MAX_ENUMERATION, retrieve_by_article

    assert indexed.execute(
        "SELECT count(*) FROM chunks WHERE regulation='AIA' AND article=3"
    ).fetchone()[0] > MAX_ENUMERATION
    assert retrieve_by_article("AIA", 3, conn=indexed) == []


def test_a_missing_provision_returns_empty_rather_than_raising(indexed):
    """A detector guessing a target from a question will guess wrong sometimes,
    and the fallback -- ordinary retrieval -- is already correct."""
    from src.query.retriever import retrieve_by_annex, retrieve_by_article

    assert retrieve_by_article("AIA", 9999, conn=indexed) == []
    assert retrieve_by_annex("GDPR", "XIV", conn=indexed) == []


def test_enumeration_needs_exactly_one_locator(indexed):
    from src.query.retriever import RetrieverError, enumerate_provision

    with pytest.raises(RetrieverError):
        enumerate_provision("AIA", conn=indexed)
    with pytest.raises(RetrieverError):
        enumerate_provision("AIA", article=6, annex="III", conn=indexed)


def test_the_enumeration_index_exists(indexed):
    """schema.sql:31-33 were columns with no index until the enumeration path
    needed them. A locator lookup by primary structure should not depend on
    corpus size, whatever the planner does at 1,108 rows."""
    names = {
        r[0] for r in indexed.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'chunks'"
        ).fetchall()
    }
    assert {"chunks_article", "chunks_annex"} <= names


def test_the_union_is_measured_but_off_in_the_live_path():
    """THE ADOPTION SWITCH, pinned so that turning it on is a deliberate edit.

    The union lifts gold-in-pool 157/203 -> 176/203 but only 100 -> 102 into the
    prompt, because ordering loss rises 51 -> 66 as retrieval loss falls 46 -> 27
    (`tests/test_reranker.py:LOSS`). It costs 2.0x rerank billing and ~240 ms. It
    is therefore committed, replayable and OFF.

    This test is not defending the value 0. It is defending the pairing: if the
    live path is ever switched on, `docs/metrics/query-path.md` has to carry the
    end-to-end measurement that justified it, which does not exist today.
    """
    from src.answer.answer_path import LEXICAL_DEPTH_LIVE

    assert LEXICAL_DEPTH_LIVE == 0, (
        "the lexical union is enabled in the live path; that needs an end-to-end "
        "measurement in docs/metrics/query-path.md, which the 2026-08-16 session "
        "explicitly did not run (2-row signal against a comparable noise floor)"
    )
