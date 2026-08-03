"""The reranker, and the ceiling arithmetic that decides what its numbers mean.

Two classes of defect live here.

The first is index mapping. Cohere returns results sorted by relevance with the
original position in `.index`, so `results[i]` is not `candidates[i]` and a
reranker that assumes it does returns real chunks with real scores attached to
the wrong text. Nothing raises.

The second is the denominator. Micro recall is capped by `sum(min(gold_i, k))`,
which is 45/51 at k=5 and 50/51 at k=10, so comparing post-rerank@5 against
pre-rerank@10 -- which the phase plan originally asked for -- charges the
reranker 9.8 percentage points of arithmetic. The tests below pin the ceilings so
that comparison cannot be reintroduced by accident.

Every test in this file runs with no container, no API key and no spend: the live
numbers are read from the committed `eval/rerank-eval.jsonl`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.query.reranker import (
    ARTIFACT,
    CANDIDATES,
    KS,
    RESOLUTION_CHUNKS,
    RerankError,
    load_artifact,
    rerank,
    rerank_detailed,
    scoreboard,
)
from src.schemas import ContextDoc

# Measured 2026-08-03 by `python -m src.query.reranker --eval --refresh`, at 512
# dims over 50 candidates. A change to any of these is a finding to be written
# down in docs/metrics/query-path.md, not a constant to be re-tuned.
QUERIES = 21
GOLD_TOTAL = 51
CAPS = {5: 45, 10: 50, 50: 51}
ORACLE = {5: 37, 10: 41}
PRE = {5: 23, 10: 28, 50: 41}
POST = {5: 27, 10: 31, 50: 41}

# The one row that makes cap@5 and cap@10 differ at all.
BIG_ROW = "ag-001"
BIG_ROW_GOLD = 11


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

def _doc(chunk_id: str, text: str = "text", score: float = 0.5) -> ContextDoc:
    return ContextDoc(
        chunk_id=chunk_id, text=text, citation_label=f"AIA Art. {chunk_id}",
        source="PASSAGE", score=score,
    )


class FakeRerankClient:
    """Returns the orderings it was constructed with, in Cohere's response shape."""

    def __init__(self, ranking: list[tuple[int, float]], search_units: float = 1.0):
        self.ranking = ranking
        self.search_units = search_units
        self.calls: list[dict] = []

    def rerank(self, **kwargs):
        self.calls.append(kwargs)
        results = [
            SimpleNamespace(index=index, relevance_score=score)
            for index, score in self.ranking
        ][: kwargs.get("top_n") or len(self.ranking)]
        return SimpleNamespace(
            results=results,
            meta=SimpleNamespace(
                billed_units=SimpleNamespace(search_units=self.search_units)
            ),
        )


class ExplodingClient:
    def rerank(self, **kwargs):
        raise AssertionError("the API was called when it should not have been")


# --------------------------------------------------------------------------
# Index mapping and the ordering contract -- pure
# --------------------------------------------------------------------------

def test_rerank_maps_every_result_index_back_to_the_candidate_it_was_sent():
    """`results[i]` is not `candidates[i]`. Assuming it is attaches real scores
    to the wrong text and raises nothing."""
    candidates = [_doc("a"), _doc("b"), _doc("c"), _doc("d")]
    client = FakeRerankClient([(2, 0.9), (0, 0.7), (3, 0.3)])
    docs = rerank("q", candidates, top_n=3, client=client)
    assert [d.chunk_id for d in docs] == ["c", "a", "d"]


def test_rerank_never_returns_a_chunk_id_it_was_not_given():
    candidates = [_doc("a"), _doc("b")]
    client = FakeRerankClient([(1, 0.9), (0, 0.1)])
    docs = rerank("q", candidates, top_n=2, client=client)
    assert {d.chunk_id for d in docs} <= {"a", "b"}


def test_rerank_overwrites_the_similarity_score_with_the_relevance_score():
    """Retaining the cosine number would let a caller read [-1,1] as [0,1]."""
    candidates = [_doc("a", score=0.61), _doc("b", score=0.60)]
    docs = rerank("q", candidates, top_n=2, client=FakeRerankClient([(1, 0.93), (0, 0.02)]))
    assert [d.score for d in docs] == [pytest.approx(0.93), pytest.approx(0.02)]


def test_rerank_does_not_mutate_the_candidates_it_was_given():
    """A long-lived worker can hand the same list to two requests. Mutating in
    place would let one request's rerank scores surface in another's retrieval."""
    candidates = [_doc("a", score=0.61), _doc("b", score=0.60)]
    rerank("q", candidates, top_n=2, client=FakeRerankClient([(1, 0.93), (0, 0.02)]))
    assert [d.score for d in candidates] == [pytest.approx(0.61), pytest.approx(0.60)]


def test_rerank_breaks_score_ties_deterministically():
    """12 corpus texts are duplicated across 24 rows and draw identical scores."""
    candidates = [_doc("a"), _doc("b"), _doc("c")]
    tied = [(2, 0.8), (0, 0.8), (1, 0.8)]
    docs = rerank("q", candidates, top_n=3, client=FakeRerankClient(tied))
    assert [d.chunk_id for d in docs] == ["a", "b", "c"]


def test_rerank_returns_an_empty_list_without_calling_the_api():
    """An empty search is still a billable request and orders nothing."""
    result = rerank_detailed("q", [], top_n=5, client=ExplodingClient())
    assert result.docs == []
    assert result.cost_usd == 0.0
    assert result.search_units == 0.0


def test_rerank_clamps_top_n_to_the_number_of_candidates():
    client = FakeRerankClient([(0, 0.9), (1, 0.5)])
    rerank("q", [_doc("a"), _doc("b")], top_n=50, client=client)
    assert client.calls[0]["top_n"] == 2


def test_rerank_refuses_a_top_n_below_one():
    with pytest.raises(RerankError, match="at least 1"):
        rerank("q", [_doc("a")], top_n=0, client=ExplodingClient())


def test_rerank_refuses_candidates_with_empty_text():
    """`text` is NOT NULL but carries no length CHECK; Cohere's 400 names an
    offset rather than a chunk."""
    with pytest.raises(RerankError, match="empty text"):
        rerank("q", [_doc("a"), _doc("b", text="  ")], client=ExplodingClient())


def test_rerank_rejects_an_index_outside_the_candidate_list():
    with pytest.raises(RerankError, match="index 9"):
        rerank("q", [_doc("a")], top_n=1, client=FakeRerankClient([(9, 0.9)]))


def test_rerank_records_the_search_units_cohere_reported():
    """The billed quantity is read off the response, never inferred from the
    document count -- Cohere splits documents over 500 tokens into extra units."""
    result = rerank_detailed(
        "q", [_doc("a")], top_n=1, client=FakeRerankClient([(0, 0.9)], search_units=3.0)
    )
    assert result.search_units == 3.0
    assert result.cost_usd == pytest.approx(3.0 * 0.002)


def test_rerank_raises_rather_than_exiting_the_process_on_an_api_error():
    from cohere.core import ApiError

    class Failing:
        def rerank(self, **kwargs):
            raise ApiError(status_code=400, body="bad request")

    with pytest.raises(RerankError, match="rerank failed"):
        rerank("q", [_doc("a")], client=Failing())


def test_rerank_raises_rather_than_exiting_when_the_api_key_is_missing(monkeypatch):
    monkeypatch.setattr("src.config.settings.cohere_api_key", "")
    with pytest.raises(RerankError, match="is not set"):
        rerank("q", [_doc("a")])


def test_rerank_asks_for_the_configured_model():
    from src.config import settings

    client = FakeRerankClient([(0, 0.9)])
    rerank("q", [_doc("a")], top_n=1, client=client)
    assert client.calls[0]["model"] == settings.model_rerank


# --------------------------------------------------------------------------
# The ceiling arithmetic -- pure, computed from the eval set
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gold_counts() -> dict[str, int]:
    from src.index.recall_harness import load_labeled_queries

    return {row["id"]: len(row["source_chunk_ids"]) for row in load_labeled_queries()}


def test_recall_at_5_cannot_exceed_45_of_51_gold_references(gold_counts):
    """A perfect reranker scores 88.2% at k=5. Comparing that against a k=10
    ceiling of 98.0% -- which the phase plan asked for -- is a 9.8pp handicap
    made of arithmetic, not of reranking."""
    assert sum(gold_counts.values()) == GOLD_TOTAL
    for k, cap in CAPS.items():
        assert sum(min(g, k) for g in gold_counts.values()) == cap


def test_ag_001_is_the_only_row_that_loses_references_between_k5_and_k10(gold_counts):
    losers = {qid: g for qid, g in gold_counts.items() if min(g, 10) > min(g, 5)}
    assert losers == {BIG_ROW: BIG_ROW_GOLD}
    assert CAPS[10] - CAPS[5] == min(BIG_ROW_GOLD, 10) - min(BIG_ROW_GOLD, 5)


def test_only_the_aggregation_stratum_is_capped_below_100_percent_at_k5():
    """So every other stratum's delta needs no ceiling adjustment at all --
    including two-hop, which is the number this step exists to move."""
    from src.index.recall_harness import load_labeled_queries

    capped = set()
    for row in load_labeled_queries():
        if min(len(row["source_chunk_ids"]), 5) < len(row["source_chunk_ids"]):
            capped.add(row["stratum"])
    assert capped == {"aggregation"}


# --------------------------------------------------------------------------
# The artifact as regression anchor -- pure, no database, no API key
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def artifact() -> list[dict]:
    return load_artifact()


@pytest.fixture(scope="module")
def board(artifact) -> dict:
    return scoreboard(artifact)


def test_the_artifact_covers_every_eval_question(artifact):
    from src.index.recall_harness import QUESTIONS

    rows = [json.loads(line) for line in
            QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {r["id"] for r in artifact} == {r["id"] for r in rows}


def test_the_artifact_gold_matches_the_eval_set(artifact):
    """The sweep copies each row's gold chunks. If the eval set is relabelled and
    the sweep is not re-run, every number in query-path.md silently becomes wrong."""
    from src.index.recall_harness import QUESTIONS

    gold = {
        json.loads(line)["id"]: json.loads(line).get("source_chunk_ids", [])
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    stale = [row["id"] for row in artifact if row["gold"] != gold[row["id"]]]
    assert stale == [], f"artifact gold is stale for {stale} -- re-run --eval --refresh"


def test_the_reranked_list_is_a_permutation_of_the_retrieved_candidates(artifact):
    """The sweep reranks all 50, so nothing may be added, dropped or invented."""
    for row in artifact:
        assert sorted(row["reranked"]) == sorted(row["retrieved"]), row["id"]
        assert len(row["retrieved"]) == CANDIDATES, row["id"]


def test_the_artifact_retrieved_at_512(artifact):
    assert {row["dim"] for row in artifact} == {512}


def test_reported_recall_never_exceeds_the_arithmetic_ceiling(board):
    for k in KS:
        assert board["overall"][f"pre@{k}"] <= board["overall"][f"cap@{k}"]
        assert board["overall"][f"post@{k}"] <= board["overall"][f"cap@{k}"]


def test_post_rerank_recall_never_exceeds_the_top_50_oracle(board):
    """Rerank cannot recover a gold chunk that is not in the candidate pool."""
    for k in (5, 10):
        assert board["overall"][f"post@{k}"] <= board["overall"][f"oracle@{k}"]


def test_the_ceilings_and_oracle_are_what_query_path_md_claims(board):
    for k in KS:
        assert board["overall"][f"cap@{k}"] == CAPS[k]
    for k, value in ORACLE.items():
        assert board["overall"][f"oracle@{k}"] == value
    assert board["gold_total"] == GOLD_TOTAL
    assert board["queries"] == QUERIES


def test_the_rerank_delta_is_what_query_path_md_claims(board):
    for k in KS:
        assert board["overall"][f"pre@{k}"] == PRE[k], f"pre@{k} moved"
        assert board["overall"][f"post@{k}"] == POST[k], f"post@{k} moved"


def test_reranking_the_whole_pool_cannot_change_recall_at_50(board):
    """A permutation of 50 candidates contains the same 50 chunks. If this delta
    is ever non-zero the sweep stopped reranking the full pool, and every other
    number in the matrix is measuring two different candidate sets."""
    assert board["overall"]["delta@50"] == 0


def test_every_per_stratum_delta_is_inside_the_pre_registered_resolution(board):
    """Pre-registered before the first rerank ran, from ADR-0004's own rule.

    The aggregate gains at k=5 (+4) and k=10 (+3) clear it; not one individual
    stratum does. So no per-stratum claim is supportable from this eval set, and
    query-path.md says so rather than narrating six one-chunk stories.
    """
    for name, stratum in board["strata"].items():
        for k in (5, 10):
            assert abs(stratum[f"delta@{k}"]) <= RESOLUTION_CHUNKS, (
                f"{name} delta@{k} is {stratum[f'delta@{k}']}, now outside the "
                f"resolution -- this is a finding, update query-path.md"
            )


def test_the_aggregate_gain_at_k5_clears_the_resolution(board):
    assert board["overall"]["delta@5"] > RESOLUTION_CHUNKS


def test_the_scoreboard_needs_nothing_but_the_artifact(artifact):
    """No database, no API key, no network -- which is what lets the metrics doc
    and these tests quote the same numbers."""
    assert scoreboard(artifact) == scoreboard(json.loads(json.dumps(artifact)))


def test_the_billed_search_units_are_recorded_for_every_call(artifact):
    """The rate in config.py has no first-party source; the quantity does.
    Storing it means correcting the rate re-prices history without re-running."""
    assert all(row["search_units"] > 0 for row in artifact)


def test_latency_excludes_nothing_silently(board, artifact):
    """3 of 23 calls stalled for seconds inside a single request on a trial key.

    They are reported by name rather than averaged into a p95 that would then be
    quoted as a Rerank 3.5 figure.
    """
    assert board["rerank_calls"] == len(artifact)
    assert {qid for qid, _ in board["rerank_stalls"]} <= {row["id"] for row in artifact}
    assert board["rerank_retried"] == [], "retried calls now exist; latency needs re-reading"


def test_the_artifact_is_beside_the_eval_set_not_under_data():
    """`data/` is gitignored wholesale. A measurement quoted in a metrics doc has
    to be readable by someone with no API key -- see router.py:66."""
    assert ARTIFACT.parent.name == "eval"
    assert ARTIFACT.exists()
