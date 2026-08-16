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

# Measured by `python -m src.query.reranker --eval --refresh`, at 512 dims over
# 50 candidates. A change to any of these is a finding to be written down in
# docs/metrics/query-path.md, not a constant to be re-tuned.
#
# RE-MEASURED 2026-08-15 ON THE 100-ROW EVAL SET. The 23-row values are kept
# beside the new ones because the comparison is the interesting part, and because
# nothing here is a re-tuning: the population changed from 21 scoreable queries
# over 51 gold references to 90 over 203, so every one of these numbers HAD to
# move. What did not have to move is the shape, and the shape is what §Findings
# below pins.
#
#   2026-08-03, 21 queries / 51 gold:  CAPS {5:45,10:50,50:51}  ORACLE {5:37,10:41}
#                                      PRE  {5:23,10:28,50:41}  POST {5:27,10:31,50:41}
QUERIES = 90
GOLD_TOTAL = 203
CAPS = {5: 190, 10: 202, 50: 203}
ORACLE = {5: 151, 10: 157}
PRE = {5: 97, 10: 117, 50: 157}
POST = {5: 100, 10: 123, 50: 157}

# THE ORACLE NOW BINDS HARDER THAN THE CAP, AND THAT IS THE HEADLINE.
#
# At 23 rows the candidate pool held 41 of 51 gold references (80%) and the cap
# was the tighter constraint at k=5. At 100 rows the pool holds 157 of 203 (77%),
# so **46 gold references are not retrievable at any k <= 50** and no reranker can
# reach them. `pre@50 == post@50 == 157 == oracle@10` is that ceiling showing up
# three times in one row of the table.
UNREACHABLE = GOLD_TOTAL - PRE[50]          # 46
assert UNREACHABLE == 46

# The one row that made cap@5 and cap@10 differ at 23 rows. It is no longer alone
# -- see test_rows_that_lose_references_between_k5_and_k10.
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


def test_recall_at_k_cannot_exceed_the_arithmetic_ceiling(gold_counts):
    """A perfect reranker scores 190/203 = 93.6% at k=5. Comparing that against a
    k=10 ceiling of 99.5% -- which the phase plan asked for -- is a handicap made
    of arithmetic, not of reranking.

    The gap narrowed with the expansion (it was 88.2% vs 98.0% at 23 rows)
    because the new questions average fewer gold chunks each, but it has not
    closed and the comparison is still not one to make.
    """
    assert sum(gold_counts.values()) == GOLD_TOTAL
    for k, cap in CAPS.items():
        assert sum(min(g, k) for g in gold_counts.values()) == cap


# The rows carrying more than 5 gold references, i.e. the only rows that can make
# cap@5 differ from cap@10. `ag-001` was alone at 23 rows; the expansion added
# three more aggregation rows, and ALL FOUR are aggregation -- which is the
# stratum definitionally built out of enumerations.
CAP_LOSERS = {"ag-001": 11, "ag-004": 7, "ag-005": 7, "ag-008": 8}


def test_rows_that_lose_references_between_k5_and_k10_are_all_aggregation(gold_counts):
    """At 23 rows this test was named `ag_001_is_the_only_row_...` and asserted a
    singleton. That was a fact about a 23-row set, not an invariant, and the
    expansion falsified it exactly as expected.

    What survives is the stronger claim: every row with more than 5 gold
    references is an `aggregation` row. If a single-hop question ever needs 6
    passages, either the question or the gold labelling is wrong.
    """
    from src.index.recall_harness import load_labeled_queries

    losers = {qid: g for qid, g in gold_counts.items() if min(g, 10) > min(g, 5)}
    assert losers == CAP_LOSERS
    assert CAPS[10] - CAPS[5] == sum(min(g, 10) - min(g, 5) for g in losers.values())

    strata = {r["stratum"] for r in load_labeled_queries() if r["id"] in losers}
    assert strata == {"aggregation"}, f"a non-aggregation row carries >5 gold: {strata}"


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


# Per-stratum deltas that clear ADR-0004's +/-2-chunk resolution, measured on the
# 100-row set. At 23 rows this set was EMPTY, and query-path.md said so: the
# aggregate cleared the resolution but not one individual stratum did.
#
# At 90 scoreable queries instead of 21, four do -- and the SIGNS are the finding:
#   cross-regulation  +5 @5, +4 @10   the reranker earning its keep
#   two-hop           +3 @10
#   three-hop         -3 @10          it costs recall here
#   aggregation       -3 @5           and here
RESOLVED_STRATA = {
    ("cross-regulation", 5): 5,
    ("cross-regulation", 10): 4,
    ("two-hop", 10): 3,
    ("three-hop", 10): -3,
    ("aggregation", 5): -3,
}


def test_the_per_stratum_deltas_that_clear_the_resolution_are_the_ones_recorded(board):
    """Pre-registered before the first rerank ran, from ADR-0004's own rule.

    THIS TEST INVERTED AT THE 100-ROW EXPANSION, AND THE INVERSION IS THE RESULT.

    It used to assert that NO per-stratum delta cleared +/-2, which is why
    query-path.md declined to narrate six one-chunk stories. With 90 scoreable
    queries instead of 21 the resolution is no longer the binding constraint, so
    per-stratum claims became supportable -- and the honest form of the test is to
    pin exactly WHICH ones rather than to keep asserting an emptiness that stopped
    being true.

    The negative entries matter as much as the positive ones. The reranker is not
    uniformly good: it buys 5 chunks on cross-regulation at k=5 and gives 3 back
    on three-hop at k=10. A benchmark reporting only the aggregate (+3 / +6) would
    hide that.
    """
    cleared = {
        (name, k): stratum[f"delta@{k}"]
        for name, stratum in board["strata"].items()
        for k in (5, 10)
        if abs(stratum[f"delta@{k}"]) > RESOLUTION_CHUNKS
    }
    assert cleared == RESOLVED_STRATA, (
        "the set of per-stratum deltas clearing the resolution moved -- that is a "
        "finding, to be written into docs/metrics/query-path.md rather than "
        "absorbed into this constant"
    )


def test_the_reranker_is_not_uniformly_positive(board):
    """The claim the README table must not overstate. Pinned so that a later sweep
    making rerank uniformly positive registers as a change rather than being
    absorbed as a tidier story."""
    assert any(v < 0 for v in RESOLVED_STRATA.values())
    assert board["overall"]["delta@5"] > 0
    assert board["overall"]["delta@10"] > 0


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
    """3 of 23 calls took seconds where the other 20 took ~300 ms on a trial key.

    They are reported by name rather than averaged into a p95 that would then be
    quoted as a Rerank 3.5 figure.

    **The `rerank_retried == []` assertion below is a tautology on this artifact
    and is kept as a regression guard rather than as evidence.** Step 6 found that
    `_call.retry.statistics` is permanently `{}` in tenacity >= 8.2.3 -- the
    `@retry` wrapper runs `copy = self.copy()` per call and assigns the copy's
    statistics to `wrapped_f.statistics`, leaving `wrapped_f.retry` as a
    controller that never executes. Every `attempts` in `rerank-eval.jsonl` was
    written by that accessor and is therefore 1 by construction, so this cannot
    currently fail. `reranker._rerank_call` now reads `_call.statistics`, so a
    re-run of `--eval --refresh` would populate the field for real and this
    assertion would start meaning what it says. `query-path.md` carries the
    correction to the conclusion that was drawn from it.
    """
    assert board["rerank_calls"] == len(artifact)
    assert {qid for qid, _ in board["rerank_stalls"]} <= {row["id"] for row in artifact}

    # THE TAUTOLOGY RESOLVED ITSELF, EXACTLY AS THE DOCSTRING ABOVE PREDICTED.
    #
    # This assertion was `== []` and could not fail, because every `attempts` in
    # the artifact was written by the broken `_call.retry.statistics` accessor and
    # was therefore 1 by construction. `reranker._rerank_call` was fixed to read
    # `_call.statistics`, and the docstring said a re-run "would populate the
    # field for real and this assertion would start meaning what it says".
    #
    # The 2026-08-15 re-run over 100 questions is that re-run. 9 of 100 calls
    # genuinely retried, and they are the same 9 that appear at the top of the
    # stall list with 51-88 s wall clock. The retry machinery is now observable,
    # which is what makes the p95 exclusion below a measurement rather than a hope.
    assert board["rerank_retried"], (
        "no retried calls recorded -- either the key stopped rate-limiting or the "
        "tenacity statistics accessor regressed to the broken one"
    )
    retried = set(board["rerank_retried"])
    worst = {qid for qid, ms in board["rerank_stalls"] if ms > 10_000}
    assert retried == worst, (
        f"every multi-second stall should be a retry and vice versa; "
        f"retried-but-fast={retried - worst}, slow-but-not-retried={worst - retried}"
    )


def test_the_artifact_is_beside_the_eval_set_not_under_data():
    """`data/` is gitignored wholesale. A measurement quoted in a metrics doc has
    to be readable by someone with no API key -- see router.py:66."""
    assert ARTIFACT.parent.name == "eval"
    assert ARTIFACT.exists()
