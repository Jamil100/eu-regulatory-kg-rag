"""The answer path's measurement, recomputed from the committed artifact.

No container, no API key, no spend. `scoreboard()` is pure by construction and
this file is what holds it to that -- every number quoted in
`docs/metrics/answer-path.md` is asserted here against
`eval/answer-eval.jsonl` alone.

THE STRUCTURAL INVARIANT IS `no arm exceeds oracle_primary`.

Step 5's `test_no_arm_exceeds_the_oracle` took this shape for the same reason: an
oracle that is a hand-typed literal can be compared to itself and pass, which is
what happened when the ceiling was computed by a script in a temp directory and
transcribed into `src/`. Here the three oracles are computed by `scoreboard()`
from the pre-registration rows, no literal of any of them exists in `src/`, and
the invariant is checked per row rather than in aggregate -- the aggregate form
permits a row that beat its own ceiling to be hidden by a row that missed.

The artifact-dependent tests skip rather than fail when the sweep has not run.
They are pinned by `test_the_artifact_is_committed_beside_the_eval_set` once it
has, so the skip cannot become permanent silently.
"""

from __future__ import annotations

import json

import pytest

from src.answer.answer_path import (
    ARTIFACT,
    PASSAGE_TOP_N,
    PREREG_KEY,
    PREREG_NS,
    AnswerPathError,
    labels_shown,
    load_questions,
    scoreboard,
)
from src.answer.context_assembly import BUDGETS, DEFAULT_BUDGET_N
from src.schemas import ContextDoc

# Read off the committed eval set. At 23 rows the router sent 10 to the graph
# (9 `both`, 1 `graph`) carrying 35 gold chunks -- quoted from
# docs/metrics/query-path.md, which measured them live.
#
# Re-read 2026-08-15 at the 100-row set: 52 routed rows (48 `both`, 4 `graph`)
# carrying 151 gold chunks. `3h-002` still carries `expected_fail` (ADR-0007).
#
# NOTE FOR THE READER OF ANY GRAPH-SIDE NUMBER IN `answer-eval.jsonl`: that
# artifact was swept against the 23-row set and its denominator is the OLD 10/35,
# not this one. The two are not comparable and the artifact must be re-swept
# before any figure computed from it is published beside a 100-row figure.
ROUTED_ROWS = 52
ROUTED_GOLD = 151

# The four refusal rows, which are reported individually and never averaged.
# `oos-002` says any citation is wrong while `hn-001`/`hn-002` say an uncited
# correct answer is only partial, so one "refusal rate" over the four would
# report two opposite failures as one success (plan-phase-3:622-624).
REFUSAL_ROWS = {"oos-001": False, "oos-002": False, "hn-001": True, "hn-002": True}

# The vector half's citable ceiling, from tests/test_reranker.py:48. It sits
# beside the graph oracles so the `both` route's ceiling reads as a union and
# never as a sum -- the two halves overlap on chunk ids by construction.
VECTOR_POST_AT_5 = 27
VECTOR_GOLD_TOTAL = 51

# --------------------------------------------------------------------------
# Measured 2026-08-05 by `python -m src.answer.answer_path --prereg` and then
# `--eval --refresh --budget <arm> -n 50`, once per arm, against the live graph
# and pgvector. A change to any of these is a finding to be written down in
# docs/metrics/answer-path.md, not a constant to be re-tuned.
# --------------------------------------------------------------------------

# All three oracles are EQUAL, which is the pre-registration's finding and the
# opposite of what the step plan predicted. The plan expected `oracle_primary`
# "well below 24", on the reasoning that `path_to_prose` sets
# `chunk_id=chunks[0]` and drops the rest. It does -- but with 389 to 642
# statements per row, every gold chunk anywhere in the provenance union is also
# the lexicographic minimum of *some* statement's own provenance. The
# `ContextDoc` boundary never cost a gold chunk.
# RE-MEASURED 2026-08-15 AT 100 ROWS, AND THE EQUALITY BROKE.
#
# At 23 rows all three oracles were equal (25/25/25) and ADR-0014 recorded that
# as a surprise: the step plan predicted `path_to_prose` keeping only `chunks[0]`
# would cost gold chunks, and it did not, because with hundreds of statements per
# row every gold chunk in the provenance union was also the lexicographic minimum
# of SOME statement's own provenance.
#
# At 52 routed rows it costs exactly one, on `th-010`: provenance 2, shown 1,
# primary 1. So the inference ADR-0014 called false is now true-but-tiny, and the
# honest form is "the ContextDoc boundary costs 1 gold chunk in 151" rather than
# either "never" or "as predicted".
ORACLES = {"oracle_provenance": 62, "oracle_shown": 61, "oracle_primary": 61}
PREREG_GOLD = 151

# The single row where the rendering boundary loses a chunk. Named, not counted.
ORACLE_BOUNDARY_ROWS = ["th-010"]

# ADR-0013 scored 9 rows (32 gold) after `bucket_of` drops `3h-002`. Recomputing
# 24 of 32 on that subset from this step's own code, through a different code
# path, is the cross-check that the two steps are measuring the same graph.
# Re-measured 2026-08-15: 51 rows / 148 gold after `bucket_of` drops `3h-002`,
# and the selector's own sweep reports oracle 62 on the same subset.
ADR_0013_ROWS = 51
ADR_0013_GOLD = 148
# Over the 51 SCORED rows (3h-002 dropped). Note these are one lower than the
# 52-row figures in ORACLES above -- `3h-002` contributes one chunk to
# `oracle_provenance` and none to the other two.
ADR_0013_ORACLE = {"oracle_provenance": 61, "oracle_shown": 60, "oracle_primary": 60}

# 3,310 statements over 10 routed rows. query-path.md publishes 2,886 over the
# 9 scored rows, and 3,310 - 424 (`3h-002`) = 2,886 exactly.
# 12,121 statements over 52 routed rows -- a mean of 233 per row and a max of
# 670. At 23 rows it was 3,310 over 10. The graph path's verbosity scales with
# the question set, which is what makes the budget arm below load-bearing.
STATEMENTS_TOTAL = 12121
TOKENS_MEAN, TOKENS_MAX, TOKENS_UNCAPPED = 20.1, 63, 272117

# Gold retention by arm at the pre-registered N, of 151 gold over 52 routed rows.
# Computed with no generation and no spend -- retention is a pure function of
# which statements survive the budget.
#
# THE ADOPTED ARM RETAINS 20 OF 61 REACHABLE GOLD CHUNKS AT N=50, AND THAT IS THE
# MOST CONSEQUENTIAL NUMBER ON THIS PAGE.
#
# `uncapped` reaches 61 -- the whole of `oracle_primary` -- because it applies no
# budget at all. `first-50`, which ADR-0014 adopted, keeps 20. At 23 rows the same
# comparison was 25 vs 8 on a much smaller denominator; the shape is unchanged and
# the stakes are four times larger, because 33 of 52 routed rows now carry more
# than 50 statements where 10 of 10 carried fewer.
#
# ADR-0014 did NOT adopt `first` on retention -- it adopted it because it was the
# only arm that produced a scored answer on every row, and `anchor` and `uncapped`
# truncated. That rationale is untouched by these numbers. But the cost of the
# choice is now four times more visible, and a hybrid arm that underperforms in
# the Phase 5 benchmark should be read against this table before anything else.
RETENTION_AT_50 = {"uncapped": 61, "anchor": 26, "first": 20, "roundrobin": 17}
RETENTION_AT_100 = {"uncapped": 61, "anchor": 44, "first": 38, "roundrobin": 33}

# Generation, on the 21 rows every arm scored (36 gold). `ag-001` and `th-001`
# are excluded because some arm truncated on them -- which is precisely the two
# rows the budget was supposed to discriminate on. See the metrics doc.
COMMON_ROWS, COMMON_GOLD = 21, 36
COMMON_CITED = {
    "uncapped": 25, "rerank": 25, "first": 24, "anchor": 24, "roundrobin": 23,
}

# ADR-0004's pre-registered resolution for this eval set, inherited by
# `reranker.RESOLUTION_CHUNKS`. A delta of <= 2 chunks is not a measured
# difference on 51 references, and it is not one on 36 either.
RESOLUTION_CHUNKS = 2

# The only arm that produced a scored answer on all 23 rows.
ARM_THAT_SCORED_EVERY_ROW = "first"

# The rows on which Command A stopped emitting structured citations and began
# writing `<co>` markup into the answer, running to MAX_TOKENS.
DEGENERATE = {"anchor": "th-001", "uncapped": "th-001", "rerank": "ag-001"}


@pytest.fixture(scope="module")
def questions() -> list[dict]:
    return load_questions()


@pytest.fixture(scope="module")
def artifact() -> list[dict]:
    if not ARTIFACT.exists():
        pytest.skip(
            f"{ARTIFACT.name} does not exist yet; run --prereg then "
            f"--eval --refresh --budget <arm>"
        )
    return [
        json.loads(line)
        for line in ARTIFACT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture(scope="module")
def board(artifact) -> dict:
    return scoreboard(artifact)


# --------------------------------------------------------------------------
# Pure -- no artifact needed
# --------------------------------------------------------------------------

def test_the_eval_set_sends_ten_rows_to_the_graph(questions):
    """The denominator every graph-side number in this step divides by."""
    routed = [r for r in questions if r["route"] in ("graph", "both")]
    assert len(routed) == ROUTED_ROWS
    assert sum(len(r["source_chunk_ids"]) for r in routed) == ROUTED_GOLD


def test_the_four_refusal_rows_all_route_vector(questions):
    """So refusal is a property of the reranked top-5 plus SYSTEM_PROMPT alone:
    the budget arms cannot move it, and it can be reported from any single arm."""
    by_id = {r["id"]: r for r in questions}
    for row_id, must_cite in REFUSAL_ROWS.items():
        assert by_id[row_id]["route"] == "vector", row_id
        assert by_id[row_id]["must_cite"] is must_cite, row_id


def test_the_refusal_rows_split_two_and_two_on_must_cite():
    """n=4, and the two halves want opposite behaviour on adjacent questions."""
    assert sorted(REFUSAL_ROWS.values()) == [False, False, True, True]


def test_labels_shown_includes_the_labels_inside_a_statements_own_text():
    """A graph statement renders up to MAX_PROVENANCE labels *in its text*, and
    all of them are strings the model can read and repeat. `uncited_labels` has
    to be asked "did the model name a provision it was not shown"."""
    doc = ContextDoc(
        chunk_id="aia-art26-para1",
        text="conduct a FRIA applies to deployer. (AIA Art. 26(1), AIA Art. 26(10), "
             "AIA Art. 26(11), +9 more)",
        citation_label="AIA Art. 26(1)",
        source="GRAPH",
        provenance=["aia-art26-para1", "aia-art26-para10", "aia-art26-para11"],
    )
    shown = labels_shown([doc])
    assert {"AIA Art. 26(1)", "AIA Art. 26(10)", "AIA Art. 26(11)"} <= shown


def test_labels_shown_does_not_turn_the_withheld_count_into_a_label():
    """`+9 more` is a count, not a label. A model that turns it into an article
    number is the exact failure `uncited_labels` exists to catch, so the tail
    must not be in the denominator."""
    doc = ContextDoc(
        chunk_id="c1", text="X applies to Y. (AIA Art. 26(1), +9 more)",
        citation_label="AIA Art. 26(1)", source="GRAPH", provenance=["c1"],
    )
    assert labels_shown([doc]) == {"AIA Art. 26(1)"}


def test_answering_with_an_unknown_budget_raises_rather_than_defaulting():
    with pytest.raises(AnswerPathError, match="is not a budget arm"):
        from src.answer.answer_path import answer

        answer("q", route="vector", budget="cheapest")


# --------------------------------------------------------------------------
# The regenerate-once loop -- never fired on the live sweep, so it is exercised
# here or it is untested. `answer()` reached `regenerated=0` on all five arms
# and all 23 rows, which is a result about the prompt and not evidence that the
# repair path works.
# --------------------------------------------------------------------------

class _TwoAnswerClient:
    """Returns a bad answer first and a good one second, in Cohere's shape.

    The first response cites a label that no document carried, which is what
    `uncited_labels` fires on; the second cites only what it was given.
    """

    def __init__(self):
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        from types import SimpleNamespace

        self.calls.append(kwargs)
        first = len(self.calls) == 1
        answer = (
            "The rule is in AIA Art. 99(3)." if first
            else "The rule is in AIA Art. 9(1)."
        )
        citation = SimpleNamespace(
            start=16, end=len(answer) - 1, text=answer[16:-1],
            sources=[SimpleNamespace(type="document", id="d0", document={})],
            content_index=0, type="TEXT_CONTENT",
        )
        counts = SimpleNamespace(input_tokens=100, output_tokens=20)
        return SimpleNamespace(
            message=SimpleNamespace(
                content=[SimpleNamespace(type="text", text=answer)],
                citations=[citation],
            ),
            finish_reason="COMPLETE",
            usage=SimpleNamespace(billed_units=counts, tokens=None),
        )


def _passage(chunk_id: str, label: str) -> ContextDoc:
    return ContextDoc(
        chunk_id=chunk_id, text=f"Text of {label}.", citation_label=label,
        source="PASSAGE", score=0.9,
    )


def test_a_bad_first_answer_is_regenerated_and_the_repair_is_recorded():
    """`regeneration_fixed` is measured, not assumed. At temperature 0 with a
    fixed seed an identical second request is a second charge for the same
    failure, so the retry appends a turn naming the defect."""
    from src.answer.answer_path import answer

    client = _TwoAnswerClient()
    result = answer(
        "q", route="vector", passages=[_passage("aia-art9-para1", "AIA Art. 9(1)")],
        client=client, budget="first",
    )
    assert len(client.calls) == 2, "the repair call was not made"
    assert result.regenerated is True
    assert result.regeneration_fixed is True
    assert result.uncited == []
    assert result.answer.endswith("AIA Art. 9(1).")


def test_the_regeneration_names_the_defect_rather_than_resending_the_request():
    """A byte-identical second request at `temperature=0, seed=42` is a
    guaranteed second charge for a guaranteed identical failure."""
    from src.answer.answer_path import answer

    client = _TwoAnswerClient()
    answer(
        "q", route="vector", passages=[_passage("aia-art9-para1", "AIA Art. 9(1)")],
        client=client, budget="first",
    )
    first, second = client.calls
    assert first["messages"] != second["messages"]
    correction = second["messages"][-1]
    assert correction["role"] == "user"
    assert "AIA Art. 99(3)" in correction["content"]
    assert "only the documents provided" in correction["content"]


def test_the_regeneration_carries_both_calls_cost():
    """Dropping the repair's charge would make the published cost per question
    short by exactly the rows that needed repairing."""
    from src.answer.answer_path import answer

    client = _TwoAnswerClient()
    result = answer(
        "q", route="vector", passages=[_passage("aia-art9-para1", "AIA Art. 9(1)")],
        client=client, budget="first",
    )
    one_call = 100 * 2.50e-6 + 20 * 10.00e-6
    assert result.cost_usd == pytest.approx(2 * one_call)


def test_a_clean_first_answer_is_never_regenerated():
    """The repair costs a full second call. It fires on a defect or not at all."""
    from src.answer.answer_path import answer

    client = _TwoAnswerClient()
    client.calls.append({})  # skip straight to the good answer
    result = answer(
        "q", route="vector", passages=[_passage("aia-art9-para1", "AIA Art. 9(1)")],
        client=client, budget="first",
    )
    assert len(client.calls) == 2  # the seeded placeholder plus one real call
    assert result.regenerated is False


def test_regeneration_can_be_switched_off_for_a_sweep_that_must_not_spend_twice():
    from src.answer.answer_path import answer

    client = _TwoAnswerClient()
    result = answer(
        "q", route="vector", passages=[_passage("aia-art9-para1", "AIA Art. 9(1)")],
        client=client, budget="first", regenerate=False,
    )
    assert len(client.calls) == 1
    assert result.regenerated is False
    assert result.uncited == ["AIA Art. 99(3)"]


def test_the_passage_slice_matches_what_the_rerank_step_measured():
    """`POST[5] = 27 of 51` is the vector half's citable ceiling and it was
    measured at k=5. Taking a different slice here would publish a ceiling that
    was never measured."""
    assert PASSAGE_TOP_N == 5


def test_the_budget_arms_are_the_five_the_adr_measures():
    assert set(BUDGETS) == {"uncapped", "first", "roundrobin", "anchor", "rerank"}


def test_the_retention_curve_brackets_the_chosen_n():
    """N is chosen off a curve, not asserted as a point. If the pre-registered Ns
    stopped straddling the default, the curve could no longer show whether the
    budget binds."""
    assert min(PREREG_NS) < DEFAULT_BUDGET_N <= max(PREREG_NS)


def test_no_oracle_is_a_literal_anywhere_in_src():
    """Step 5's own recurrence, checked mechanically rather than remembered.

    The ceiling and oracle were computed by scripts in a temp directory outside
    the repo and transcribed into `src/` as constants, so
    `test_rules_reaches_the_oracle` compared a constant to itself. The oracles
    for this step are computed by `preregister()` and summed by `scoreboard()`;
    the published values live in this file as assertions, never as sources.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for path in src.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or '"""' in stripped:
                continue
            if "ORACLE" in stripped and "=" in stripped and "oracle_" not in stripped:
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], f"an oracle typed as a literal in src/: {offenders}"


# --------------------------------------------------------------------------
# The artifact -- pure, no database, no API key
# --------------------------------------------------------------------------

def test_the_artifact_is_committed_beside_the_eval_set():
    """`data/` is gitignored wholesale. A measurement quoted in a metrics doc has
    to be readable by someone with no API key -- see router.py:66."""
    assert ARTIFACT.parent.name == "eval"


def test_the_artifact_carries_the_pre_registration(artifact):
    """Computed before any arm was written, and stored beside the arms so a
    reader cannot pair an arm with a stale set of oracles."""
    prereg = [row for row in artifact if row.get("budget") == PREREG_KEY]
    assert prereg, "no pre-registration rows -- run --prereg"
    assert len(prereg) == ROUTED_ROWS


def test_every_artifact_row_names_its_budget(artifact):
    """The file holds every arm plus the pre-registration, keyed the way
    `selector-eval.jsonl` holds both selectors. An unkeyed row would be
    attributed to whichever arm read it."""
    assert all(row.get("budget") for row in artifact)
    assert {row["budget"] for row in artifact} <= {PREREG_KEY, *BUDGETS}


def test_the_artifact_gold_matches_the_eval_set(artifact, questions):
    """If the eval set is relabelled and the sweep is not re-run, every number in
    answer-path.md silently becomes wrong."""
    gold = {r["id"]: r["source_chunk_ids"] for r in questions}
    stale = [
        row["id"] for row in artifact
        if sorted(row["gold"]) != sorted(gold[row["id"]])
    ]
    assert stale == [], f"artifact gold is stale for {stale} -- re-run the sweep"


def test_the_three_oracles_are_computed_by_the_scoreboard(board):
    """Not recalled, not transcribed. `scoreboard` is pure, so this is the same
    computation the metrics doc quotes."""
    assert set(board["oracles"]) == {
        "oracle_provenance", "oracle_shown", "oracle_primary"
    }
    assert all(isinstance(v, int) for v in board["oracles"].values())


def test_the_oracles_are_ordered_by_what_each_one_permits(board):
    """`provenance` is a union over every chunk any returned row asserts, `shown`
    is what MAX_PROVENANCE rendering put in front of the model, and `primary` is
    `chunk_id` alone. Each is a subset of the one before it, so the inequality is
    structural: if it ever inverts, one of the three was computed against the
    wrong set."""
    o = board["oracles"]
    assert o["oracle_primary"] <= o["oracle_shown"] <= o["oracle_provenance"]


def test_no_oracle_exceeds_the_gold_available(board):
    assert board["oracles"]["oracle_provenance"] <= board["prereg_gold"]
    assert board["prereg_gold"] == ROUTED_GOLD


def test_no_arm_exceeds_the_oracle_on_any_row(board):
    """THE STRUCTURAL INVARIANT. Per row, not in aggregate: the aggregate form
    lets a row that beat its own ceiling be hidden by a row that missed."""
    for arm in board["arms"]:
        assert board[arm]["exceeds_oracle"] == [], (
            f"{arm} cited gold on {board[arm]['exceeds_oracle']} that the uncapped "
            f"graph path never produced -- the measurement is not sound"
        )


def test_the_scoreboard_needs_nothing_but_the_artifact(artifact):
    """No database, no API key, no network -- which is what lets the metrics doc
    and these tests quote the same numbers."""
    assert scoreboard(artifact) == scoreboard(json.loads(json.dumps(artifact)))


def test_the_non_discriminating_rows_are_published_with_their_gold(board):
    """Rows where every statement fits inside the budget cannot tell two arms
    apart. Publishing the discriminating denominator beside the total is what
    stops a tie on those rows reading as agreement."""
    assert board["non_discriminating"], "no N was recorded"
    for n, ids in board["non_discriminating"].items():
        assert n in board["non_discriminating_gold"]
        assert set(ids) <= set(board["statements_per_row"])


def test_first_and_roundrobin_cannot_differ_on_the_one_call_rows(board):
    """Arithmetic, stated before the sweep. A tie on these rows is not evidence
    that the two arms agree."""
    assert board["one_call_rows"] == sorted(board["one_call_rows"])
    for row_id in board["one_call_rows"]:
        assert board["calls_per_row"][row_id] <= 1


def test_max_tokens_rows_are_excluded_from_the_published_rates(board, artifact):
    """A truncated answer can lose the second half of its last citation, so its
    span and label rates measure the token limit rather than the model. Same
    treatment reranker.py:400 gives `attempts != 1` -- excluded and named, never
    silently dropped."""
    for arm in board["arms"]:
        rows = [
            r for r in artifact
            if r.get("budget") == arm and r.get("finish_reason") == "MAX_TOKENS"
        ]
        assert sorted(board[arm]["truncated"]) == sorted(r["id"] for r in rows)
        assert board[arm]["scored"] + len(board[arm]["truncated"]) + len(
            board[arm]["errors"]
        ) == board[arm]["n_rows"]


def test_the_four_refusal_rows_are_reported_individually_and_split_by_must_cite(board):
    """Never as one rate. Two of them want zero citations and two want a cited
    answer, and a single number over the four would report a system that got two
    right in opposite ways as identical to one that got all four right."""
    for arm in board["arms"]:
        refusal = board[arm]["refusal"]
        if not refusal:
            continue
        assert set(refusal) >= set(REFUSAL_ROWS), (
            f"{arm} is missing refusal rows: {set(REFUSAL_ROWS) - set(refusal)}"
        )
        for row_id, want in REFUSAL_ROWS.items():
            assert refusal[row_id]["must_cite"] is want
        assert not any(key.endswith("refusal_rate") for key in board[arm])


def test_the_vector_ceiling_is_a_union_with_the_graph_oracle_not_a_sum(board):
    """The two halves overlap on chunk ids by construction -- a GRAPH and a
    PASSAGE document routinely name the same chunk -- so adding 27 to
    `oracle_primary` would double-count every shared gold chunk and publish a
    ceiling above the gold available."""
    assert VECTOR_POST_AT_5 <= VECTOR_GOLD_TOTAL
    for arm in board["arms"]:
        assert board[arm]["gold_cited"] <= board[arm]["gold_total"]


def test_every_cited_chunk_id_was_in_the_assembled_set(artifact):
    """The end-to-end version of `validate()`, asserted from the artifact rather
    than trusted from the code that produced it."""
    for row in artifact:
        if row.get("budget") == PREREG_KEY or row.get("error"):
            continue
        assert row.get("rejected") == [], (
            f"{row['id']} ({row['budget']}) cited {row['rejected']}, which the "
            f"assembled set did not contain"
        )


def test_every_surviving_citation_quotes_the_span_it_points_at(artifact):
    """`answer[start:end] == citation.text` for every citation in the artifact --
    the `content_index` rebasing bug, checked against real responses."""
    from src.answer.citation_validator import span_defects
    from src.schemas import Citation

    for row in artifact:
        if row.get("budget") == PREREG_KEY or row.get("error"):
            continue
        citations = [Citation.model_validate(c) for c in row.get("citations", [])]
        defects = span_defects(row["answer"], citations)
        assert defects == [], f"{row['id']} ({row['budget']}) span defects at {defects}"


def test_a_regenerated_row_records_whether_the_regeneration_fixed_anything(artifact):
    """At temperature 0 with a fixed seed an identical second request is a
    guaranteed second charge for a guaranteed identical failure, so the repair
    has to be measured rather than assumed to work."""
    for row in artifact:
        if row.get("budget") == PREREG_KEY or row.get("error"):
            continue
        if row.get("regenerated"):
            assert "regeneration_fixed" in row, row["id"]
        else:
            assert not row.get("regeneration_fixed"), row["id"]


# --------------------------------------------------------------------------
# The published numbers, asserted against the artifact
# --------------------------------------------------------------------------

def test_the_three_oracles_are_what_answer_path_md_claims(board):
    assert board["oracles"] == ORACLES
    assert board["prereg_gold"] == PREREG_GOLD


def test_the_scored_subset_reproduces_adr_0013s_twenty_four_of_thirty_two(artifact):
    """THE CROSS-CHECK. ADR-0013 measured 24 of 32 gold chunks reachable by the
    rules selector, over the 9 rows left after `bucket_of` drops `3h-002`. This
    step recomputes the same figure through an entirely different code path --
    `graph_search` -> `ContextDoc.provenance` rather than `provenance_of` over
    raw rows. Agreement to the chunk is what says the two steps are describing
    the same graph, and disagreement would mean one of them is wrong."""
    prereg = [
        row for row in artifact
        if row.get("budget") == PREREG_KEY and row["id"] != "3h-002"
    ]
    assert len(prereg) == ADR_0013_ROWS
    assert sum(len(row["gold"]) for row in prereg) == ADR_0013_GOLD
    for name, expected in ADR_0013_ORACLE.items():
        assert sum(len(row[name]) for row in prereg) == expected, name
    # THE CROSS-CHECK, AND IT PAIRS WITH THE SELECTOR'S *RULES* ARM, NOT ITS ORACLE.
    #
    # The two are different quantities and pairing them wrongly is an easy mistake
    # to encode: `template_selector`'s `oracle` is the best single (template,
    # anchor) per row chosen with the gold visible, while this step's
    # `oracle_provenance` is what the RULES-selected plan actually asserted. They
    # coincided at 23 rows (24 == 24), which is what made the cross-check look
    # like an identity; at 100 rows the selector's oracle is 62 and its rules arm
    # is 61, and 61 is the number this step must reproduce.
    from src.query import template_selector as ts

    other = ts.scoreboard(load_questions(), ts.load_artifact())
    assert ADR_0013_ORACLE["oracle_provenance"] == other["rules"]["gold_hit"], (
        "the two steps disagree about what the rules-selected plan reaches -- "
        "one of them is wrong, and they run through entirely different code"
    )
    assert other["oracle"] > other["rules"]["gold_hit"], (
        "the selector's oracle no longer exceeds its rules arm; if these are "
        "equal again the pairing above stops being a real cross-check"
    )


def test_the_statement_count_reconciles_with_query_path_md(board, artifact):
    """12,121 statements over 52 routed rows, and the scored subset is that minus
    exactly `3h-002`. Two documents quoting two numbers that do not reconcile is
    how a metrics directory stops being auditable."""
    assert board["statements_total"] == STATEMENTS_TOTAL
    expected_fail = next(
        row for row in artifact
        if row.get("budget") == PREREG_KEY and row["id"] == "3h-002"
    )
    scored = STATEMENTS_TOTAL - expected_fail["statements"]
    assert scored > 0
    assert scored == sum(
        row["statements"] for row in artifact
        if row.get("budget") == PREREG_KEY and row["id"] != "3h-002"
    )


def test_the_token_measurement_is_what_the_budget_was_chosen_against(board):
    """N was pre-registered from a token budget, so the token budget has to be
    published or N is a tuned parameter wearing a pre-registration's clothes."""
    assert board["tokens_mean"] == TOKENS_MEAN
    assert board["tokens_max"] == TOKENS_MAX
    assert board["tokens_uncapped_total"] == TOKENS_UNCAPPED


def test_the_retention_curve_is_what_adr_0014_adopts_on(board):
    for n, expected in ((50, RETENTION_AT_50), (100, RETENTION_AT_100)):
        got = {arm: board["arm_retention"][arm][str(n)] for arm in board["free_arms"]}
        assert got == expected, f"retention at N={n} moved"


def test_the_budget_costs_more_gold_than_any_arm_difference_recovers(board):
    """THE FINDING. At N=50 the best capped arm keeps 10 of the 25 chunks the
    uncapped path reaches. The 15-chunk gap to the ceiling is five times the
    2-chunk gap between the best and worst capped arm, so the budget itself is
    the lever and the ranking inside it is not."""
    ceiling = RETENTION_AT_50["uncapped"]
    capped = {k: v for k, v in RETENTION_AT_50.items() if k != "uncapped"}
    best, worst = max(capped.values()), min(capped.values())
    assert ceiling - best > (best - worst) * 2


# `anchor` minus `first`, at each pre-registered N, on the 100-row set.
ANCHOR_OVER_FIRST = {50: 6, 100: 6}


def test_anchor_now_beats_the_adopted_constant_outside_the_resolution(board):
    """THIS TEST INVERTED AT 100 ROWS, AND ADR-0014 SHOULD BE READ WITH IT.

    At 23 rows `anchor` was +2 over `first` at N=50 and +1 at N=100. ADR-0004
    declares a +-2 chunk resolution for this eval set, so +2 is *at* the boundary
    and the house rule reads that as no measured difference -- which is why the
    old form of this test asserted `anchor - first <= 2` and concluded "anchor did
    not earn the adoption".

    At 52 routed rows the gap is +6 at both N=50 and N=100, outside the resolution
    at both. `anchor` is now measurably the better budget on retention.

    **ADR-0014's adoption still stands, and it does not rest on this.** `first`
    was adopted because it was the only arm that produced a scored answer on every
    row -- `anchor` and `uncapped` ran to MAX_TOKENS on `th-001`. A budget that
    retains more gold and then truncates the answer retains nothing. What has
    changed is that the trade is now visible and priced: adopting `first` costs 6
    gold chunks of retention, where at 23 rows it cost an amount indistinguishable
    from zero.

    This is a finding for ADR-0014, not a constant to re-tune, and re-running the
    five arms at 100 rows is what would settle whether `anchor` still truncates.
    """
    from src.query.reranker import RESOLUTION_CHUNKS as INHERITED

    assert RESOLUTION_CHUNKS == INHERITED
    for n, curve in ((50, RETENTION_AT_50), (100, RETENTION_AT_100)):
        gap = curve["anchor"] - curve["first"]
        assert gap == ANCHOR_OVER_FIRST[n], f"the anchor-vs-first gap at N={n} moved"
        assert gap > RESOLUTION_CHUNKS


def test_roundrobin_is_measurably_worse_than_the_constant(board):
    """-4 at N=50 and -3 at N=100, both outside the resolution. The one arm with
    a structural reason to beat `first` is the one that measurably loses to it,
    and that is a publishable negative result rather than a rounding error."""
    assert RETENTION_AT_50["first"] - RETENTION_AT_50["roundrobin"] > RESOLUTION_CHUNKS


def test_the_common_denominator_excludes_the_rows_the_budget_discriminates_on(board):
    """THE DENOMINATOR TRAP, PINNED.

    Each arm drops its own MAX_TOKENS rows and they are different rows, so the
    per-arm `gold cited` figures (28/51, 28/47, 25/40) were never measured on the
    same set. Imposing a common denominator fixes the comparison and destroys it:
    the rows every arm scored exclude `ag-001` and `th-001`, which are two of the
    three largest graph rows -- exactly where a budget can bite. All five arms
    then land within 2 chunks, inside the resolution.
    """
    assert board["common"]["n"] == COMMON_ROWS
    assert board["common"]["gold"] == COMMON_GOLD
    assert set(board["common"]["excluded"]) == {"ag-001", "th-001"}
    got = {arm: board[arm]["common_gold_cited"] for arm in board["arms"]}
    assert got == COMMON_CITED
    assert max(got.values()) - min(got.values()) <= RESOLUTION_CHUNKS


def test_only_the_constant_produced_a_scored_answer_on_every_row(board):
    """Which is the adoption, and it is not a retention argument. `anchor` and
    `uncapped` truncate on `th-001`, `rerank` on `ag-001`; `first` truncates
    nowhere."""
    complete = [
        arm for arm in board["arms"]
        if not board[arm]["truncated"] and not board[arm]["errors"]
    ]
    assert ARM_THAT_SCORED_EVERY_ROW in complete
    for arm, row_id in DEGENERATE.items():
        assert board[arm]["truncated"] == [row_id], arm


def test_every_truncated_row_leaked_citation_markup_into_the_answer(artifact):
    """THE FAILURE MODE, NAMED.

    All three truncations share one cause and it is not "the answer was long".
    Command A leaks its own citation training format -- `<co>...</co>` -- into
    the answer *text*, and once it starts it does not stop before `max_tokens`.
    The structured `citations` list is unaffected and can be large, so a row can
    carry 119 citations and still be a collapsed answer: `finish_reason` and this
    check are what tell them apart, not the citation count.
    """
    for arm, row_id in DEGENERATE.items():
        row = next(r for r in artifact if r.get("budget") == arm and r["id"] == row_id)
        assert row["finish_reason"] == "MAX_TOKENS", (arm, row_id)
        assert "<co" in row["answer"], (arm, row_id)


def test_the_anchor_arm_collapses_early_where_the_others_run_out_at_the_end(artifact):
    """The three truncations are not equally bad, and averaging them would hide
    the one that matters.

    `uncapped`/th-001 and `rerank`/ag-001 are near-complete answers -- the markup
    appears 94% and 97% of the way in, wrapping a final bullet -- and
    `rerank`/ag-001 still lands 4 gold chunks. `anchor`/th-001 collapses at 13%
    into malformed `<co<co<co` repetition and cites no gold at all. So `anchor`
    is the arm that produced an unusable answer, and it is also the arm whose
    documents are 48-of-50 the same sentence frame.
    """
    def markup_at(arm: str, row_id: str) -> float:
        row = next(r for r in artifact if r.get("budget") == arm and r["id"] == row_id)
        return row["answer"].find("<co") / len(row["answer"])

    assert markup_at("anchor", "th-001") < 0.25
    assert markup_at("uncapped", "th-001") > 0.85
    assert markup_at("rerank", "ag-001") > 0.85

    anchor_row = next(
        r for r in artifact if r.get("budget") == "anchor" and r["id"] == "th-001"
    )
    assert anchor_row["gold_cited"] == []
    assert "<co<co" in anchor_row["answer"], "the malformed repetition is the signature"


def test_the_adopted_budget_is_the_arm_this_file_says_won():
    """`context_assembly.ADOPTED_BUDGET` is set by ADR-0014 from the measurement.
    Change it there and here together, or the ADR stops describing the code."""
    from src.answer.context_assembly import ADOPTED_BUDGET

    assert ADOPTED_BUDGET == ARM_THAT_SCORED_EVERY_ROW


def test_attempts_is_recorded_per_row_so_a_rate_limited_row_is_visible(artifact):
    """Inferring a rate limit from a bad score is what scored `th-004` a zero in
    Step 5. The field only became capable of exceeding 1 in this step -- see
    `tests/test_generate.py` on `_call.retry.statistics`."""
    for row in artifact:
        if row.get("budget") == PREREG_KEY or row.get("error"):
            continue
        assert isinstance(row.get("attempts"), int) and row["attempts"] >= 1
