"""The judge decides every accuracy cell in the README table, so it is tested
like the instrument it is.

No API key and no network: the grading call is exercised through a fake client
that returns a canned reply, and everything else here is pure. The two things
worth catching are the per-stratum citation convention and the verdict cap, both
of which are deterministic and both of which would otherwise be assertions living
only in a docstring.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from eval.judge import (
    VERDICTS,
    JudgeError,
    agreement,
    build_prompt,
    citation_defect,
    holdout,
    judge,
)
from eval.run_benchmark import load_questions


def make_row(**over) -> dict:
    row = {
        "id": "t-001",
        "stratum": "single-hop",
        "question": "What does Art. 9(1) require?",
        "gold": "A risk management system shall be established.",
        "grading_rule": "Must state that a risk management system is required.",
        "must_cite": True,
    }
    row.update(over)
    return row


class FakeClient:
    """Returns one canned reply and records the messages it was sent."""

    def __init__(self, verdict="correct", reason="matches the gold", raw=None,
                 input_tokens=100, output_tokens=20):
        self.raw = raw if raw is not None else json.dumps(
            {"verdict": verdict, "reason": reason}
        )
        self.seen: list[list[dict]] = []
        self._usage = SimpleNamespace(
            billed_units=SimpleNamespace(
                input_tokens=input_tokens, output_tokens=output_tokens
            ),
        )

    def chat(self, **kwargs):
        self.seen.append(kwargs["messages"])
        assert kwargs["temperature"] == 0, "the judge must be pinned to temperature 0"
        return SimpleNamespace(
            message=SimpleNamespace(content=[SimpleNamespace(text=self.raw)]),
            usage=self._usage,
        )


# --------------------------------------------------------------------------
# The citation convention -- per stratum, never global
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stratum", ["out-of-scope", "unanswerable"])
def test_a_refusal_row_that_cites_anything_is_a_defect(stratum):
    row = make_row(stratum=stratum, must_cite=False)
    assert citation_defect(row, []) == ""
    assert "must cite nothing" in citation_defect(row, [{"chunk_id": "x"}])


def test_a_hard_negative_row_that_cites_nothing_is_a_defect():
    """The opposite failure from the one above, and the reason `citation_defect`
    returns a named string rather than a bool: a single boolean would report a
    system that made both failures as internally consistent."""
    row = make_row(stratum="hard-negative", must_cite=True)
    assert citation_defect(row, [{"chunk_id": "x"}]) == ""
    assert "no citation" in citation_defect(row, [])


def test_hard_negative_is_not_treated_as_a_no_citation_refusal():
    """docs/metrics/eval-set.md:63-67 -- averaging the three modes hides which
    behaviour failed. This pins that hard-negative is on the other side."""
    from eval.judge import REFUSE_UNCITED

    assert "hard-negative" not in REFUSE_UNCITED


def test_a_normal_row_that_cites_nothing_is_a_defect_only_if_it_must_cite():
    assert citation_defect(make_row(must_cite=True), []) != ""
    assert citation_defect(make_row(must_cite=False), []) == ""


# --------------------------------------------------------------------------
# The verdict cap -- applied in code, not requested in the prompt
# --------------------------------------------------------------------------

def test_a_refusal_that_cites_cannot_score_correct_refusal():
    row = make_row(stratum="out-of-scope", must_cite=False)
    client = FakeClient(verdict="correct_refusal")
    result = judge(row, "This is outside the corpus.", [{"chunk_id": "x"}], client=client)
    assert result.verdict == "partially_correct"
    assert result.capped_from == "correct_refusal"
    assert result.defect


def test_a_must_cite_row_with_no_citation_cannot_score_correct():
    """The case docs/metrics/answer-path.md records: right prose, no grounding."""
    result = judge(make_row(), "A risk management system is required.", [],
                   client=FakeClient(verdict="correct"))
    assert result.verdict == "partially_correct"
    assert result.capped_from == "correct"


def test_the_cap_never_improves_a_wrong_verdict():
    result = judge(make_row(stratum="out-of-scope", must_cite=False),
                   "The answer is 42.", [{"chunk_id": "x"}],
                   client=FakeClient(verdict="wrong"))
    assert result.verdict == "wrong"
    assert result.capped_from == ""


def test_a_clean_row_is_not_capped():
    result = judge(make_row(), "A risk management system is required.",
                   [{"chunk_id": "aia-art9-para1"}], client=FakeClient(verdict="correct"))
    assert result.verdict == "correct"
    assert result.capped_from == ""
    assert result.defect == ""


# --------------------------------------------------------------------------
# The prompt carries what the grade depends on
# --------------------------------------------------------------------------

def test_the_grading_rule_reaches_the_prompt():
    """The whole point of widening the signature. A judge that never sees the
    per-question rule invents its own partial/wrong boundary each time."""
    row = make_row(grading_rule="Omitting 'whichever is higher' is partial.")
    client = FakeClient()
    judge(row, "an answer", [{"chunk_id": "x"}], client=client)
    user_turn = client.seen[0][-1]["content"]
    assert "whichever is higher" in user_turn
    assert "decisive" in user_turn


def test_a_row_without_a_grading_rule_is_refused():
    with pytest.raises(JudgeError, match="grading_rule"):
        judge(make_row(grading_rule="  "), "an answer", [], client=FakeClient())


def test_the_defect_is_stated_to_the_judge_rather_than_left_to_be_inferred():
    row = make_row(stratum="unanswerable", must_cite=False)
    client = FakeClient(verdict="correct_refusal")
    judge(row, "The text states no period.", [{"chunk_id": "x"}], client=client)
    assert "CITATION CHECK" in client.seen[0][-1]["content"]


def test_build_prompt_handles_an_empty_answer():
    turn = build_prompt(make_row(), "", "")[-1]["content"]
    assert "produced no answer" in turn


# --------------------------------------------------------------------------
# Parsing -- a bad reply is recorded, never coerced
# --------------------------------------------------------------------------

def test_a_fenced_json_reply_still_parses():
    raw = '```json\n{"verdict": "correct", "reason": "fine"}\n```'
    result = judge(make_row(), "a", [{"chunk_id": "x"}], client=FakeClient(raw=raw))
    assert result.verdict == "correct"


def test_json_wrapped_in_prose_still_parses():
    raw = 'Here is my grade: {"verdict": "wrong", "reason": "fabricated a figure"} -- done.'
    result = judge(make_row(), "a", [{"chunk_id": "x"}], client=FakeClient(raw=raw))
    assert result.verdict == "wrong"


def test_an_unknown_verdict_raises_rather_than_being_coerced():
    """A near-miss label quietly mapped to a real one would make the judge's
    output format unfalsifiable."""
    raw = '{"verdict": "partial", "reason": "close"}'
    with pytest.raises(JudgeError, match="unknown verdict"):
        judge(make_row(), "a", [{"chunk_id": "x"}], client=FakeClient(raw=raw))


def test_an_unparseable_reply_raises_rather_than_defaulting():
    with pytest.raises(JudgeError, match="not JSON"):
        judge(make_row(), "a", [{"chunk_id": "x"}], client=FakeClient(raw="looks good to me"))


def test_cost_is_priced_from_billed_units():
    result = judge(make_row(), "a", [{"chunk_id": "x"}],
                   client=FakeClient(input_tokens=1000, output_tokens=100))
    # command-a-03-2025 at 2.50/1M in, 10.00/1M out.
    assert result.cost_usd == pytest.approx(1000 * 2.5e-6 + 100 * 1e-5)


# --------------------------------------------------------------------------
# Agreement
# --------------------------------------------------------------------------

def test_agreement_counts_matches_and_names_disagreements():
    board = agreement(
        judged={"a": "correct", "b": "wrong", "c": "correct"},
        hand={"a": "correct", "b": "partially_correct"},
    )
    assert board["n"] == 2
    assert board["matches"] == 1
    assert board["rate"] == 0.5
    assert board["disagreements"] == [
        {"id": "b", "hand": "partially_correct", "judge": "wrong"}
    ]


def test_a_hand_label_with_no_judgement_is_reported_not_dropped():
    """A silently shrinking denominator is how an agreement figure flatters
    itself."""
    board = agreement(judged={"a": "correct"}, hand={"a": "correct", "z": "wrong"})
    assert board["missing"] == ["z"]
    assert board["n"] == 1


def test_agreement_on_an_empty_hand_sample_is_none_not_one():
    board = agreement(judged={"a": "correct"}, hand={})
    assert board["rate"] is None


# --------------------------------------------------------------------------
# The judge's verdict space matches what the eval set expects of it
# --------------------------------------------------------------------------

def test_every_eval_row_carries_what_the_judge_requires():
    """A row missing any of these cannot be graded, so it would silently become
    an ungraded row in the benchmark rather than a loud failure here."""
    missing = [
        row["id"] for row in load_questions()
        if not all(str(row.get(f, "")).strip() for f in ("question", "gold", "grading_rule"))
    ]
    assert not missing, f"rows the judge cannot grade: {missing}"


def test_the_four_verdicts_are_the_ones_the_roadmap_names():
    assert set(VERDICTS) == {"correct", "partially_correct", "wrong", "correct_refusal"}


# --------------------------------------------------------------------------
# The hand-graded holdout -- chosen before any verdict is read
# --------------------------------------------------------------------------

def test_the_holdout_is_deterministic():
    """WHICH 20% has to be fixed before anyone reads a grade, or the sample
    becomes the rows whose verdicts looked interesting."""
    rows = load_questions()
    assert holdout(rows) == holdout(rows)
    assert holdout(list(reversed(rows))) == holdout(rows), "must not depend on file order"


def test_the_holdout_is_about_twenty_percent_and_covers_every_stratum():
    """Roadmap S5.3 asks for 20%. Every stratum must appear: a holdout with no
    refusal row cannot detect the failure the refusal strata exist to measure."""
    import collections

    rows = load_questions()
    ids = set(holdout(rows))
    assert 0.15 * len(rows) <= len(ids) <= 0.30 * len(rows)
    strata = {r["stratum"] for r in rows if r["id"] in ids}
    assert strata == {r["stratum"] for r in rows}, f"strata missing: {strata}"
    # The floor keeps the 5-row strata represented rather than rounded away.
    counts = collections.Counter(r["stratum"] for r in rows if r["id"] in ids)
    assert min(counts.values()) >= 1


def test_the_holdout_covers_both_must_cite_conventions():
    """The judge's hardest call is the must_cite split between `hard-negative`
    and the other two refusal modes, so the sample has to contain both sides."""
    rows = load_questions()
    ids = set(holdout(rows))
    sampled = [r for r in rows if r["id"] in ids]
    assert any(r["must_cite"] for r in sampled)
    assert any(not r["must_cite"] for r in sampled)


# --------------------------------------------------------------------------
# The blind half of the agreement protocol, enforced rather than trusted
# --------------------------------------------------------------------------

def test_grade_holdout_never_prints_a_verdict():
    """An agreement figure produced after seeing the machine label measures
    anchoring, not agreement. `grade_holdout` is the mechanism that keeps the
    hand pass blind, so its omission is asserted here rather than left to the
    discipline of whoever runs it."""
    from eval.grade_holdout import WITHHELD, blind_rows

    artifact = [{
        "system": "hybrid", "mode": "replay", "id": "sh-001",
        "answer": "an answer", "cited_chunk_ids": ["c1"], "gold_cited": ["c1"],
        "verdict": "correct", "judge_reason": "because", "judge_defect": "",
        "judge_capped_from": "", "judge_error": None,
    }]
    rows = blind_rows(artifact, ["sh-001"], "hybrid")
    assert rows, "the holdout row was dropped entirely"
    for field in WITHHELD:
        assert field not in rows[0], f"{field} leaked into the blind payload"
    assert rows[0]["answer"] == "an answer", "the answer itself must survive"


def test_grade_holdout_withholds_every_judgement_field():
    """If a new judgement field is added to the artifact it must be added to
    WITHHELD too; this pins the ones that exist today."""
    from eval.grade_holdout import WITHHELD

    assert set(WITHHELD) >= {"verdict", "judge_reason", "judge_defect"}


def test_grade_holdout_only_returns_replayed_rows_of_one_system():
    """The live pass re-answers every question, so mixing modes would show the
    grader two different answers for one row."""
    from eval.grade_holdout import blind_rows

    artifact = [
        {"system": "hybrid", "mode": "replay", "id": "a", "answer": "replayed"},
        {"system": "hybrid", "mode": "live", "id": "a", "answer": "live"},
        {"system": "vector", "mode": "replay", "id": "a", "answer": "other system"},
    ]
    rows = blind_rows(artifact, ["a"], "hybrid")
    assert len(rows) == 1
    assert rows[0]["answer"] == "replayed"
