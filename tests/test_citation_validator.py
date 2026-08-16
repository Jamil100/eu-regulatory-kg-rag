"""The three checks, and which of them can actually fail.

No container, no API key, no spend.

`validate()` is the one the plan names and the one that cannot return False on
the happy path -- `generate()` maps source ids through the reverse map of the
documents it just sent, and `retrieved_ids` is those same documents' chunk ids,
so membership holds by construction. Publishing the resulting 0% would be
`docs/failure-notes.md`'s "A metric that looked like success" for a fourth time.
The tests below therefore exercise it against the four ways it *can* fire, and
spend most of their weight on the two checks with teeth:

  `span_defects`   fails on a real response, and is what catches the
                   `content_index` rebasing bug in `generate.py`.
  `uncited_labels` is the one that will be non-zero, and the one Phase 5 cares
                   about, because `eval-questions.jsonl` grades on label strings.
"""

from __future__ import annotations

import json

import pytest

from src.answer.citation_validator import (
    LABEL_RE,
    normalise_label,
    span_defects,
    uncited_labels,
    validate,
)
from src.query.router import QUESTIONS
from src.schemas import Citation

# Read off the committed eval set 2026-08-05 at 23 questions (41 labels) and
# re-read 2026-08-15 at 100 (101 labels), in three shapes: `AIA Art. 9(1)`, the
# unparagraphed `AIA Art. 60`, and the nested `AIA Annex III(1)(a)`.
#
# The expansion added no fourth shape, which is the substantive result here --
# all 101 still `fullmatch` unchanged, so `LABEL_RE` needed no widening for the
# 60 new labels including the `AIA Annex XI` and `AIA Annex III(4)` forms.
DISTINCT_GOLD_LABELS = 101


def cite(start: int, end: int, text: str, chunk_id: str = "c1") -> Citation:
    return Citation(chunk_id=chunk_id, start=start, end=end, text=text)


@pytest.fixture(scope="module")
def gold_labels() -> set[str]:
    labels: set[str] = set()
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            labels.update(json.loads(line).get("citations", []))
    return labels


# --------------------------------------------------------------------------
# validate -- membership, and the four ways it fires
# --------------------------------------------------------------------------

def test_validate_is_true_when_every_cited_chunk_was_retrieved():
    citations = [cite(0, 3, "The", "a"), cite(4, 9, "rules", "b")]
    assert validate(citations, {"a", "b", "c"}) is True


def test_validate_is_false_when_a_cited_chunk_was_not_retrieved():
    """The shape of "an id the model generated rather than echoed" and of "a
    citation surviving a regeneration against a rebuilt document list"."""
    citations = [cite(0, 3, "The", "a"), cite(4, 9, "rules", "invented")]
    assert validate(citations, {"a", "b"}) is False


def test_validate_is_true_on_an_answer_with_no_citations():
    """`oos-001` and `oos-002` are graded on producing exactly this. A refusal is
    not a validation failure."""
    assert validate([], {"a"}) is True


def test_validate_is_false_against_an_empty_retrieved_set():
    assert validate([cite(0, 1, "A", "a")], set()) is False


def test_validate_checks_every_citation_and_not_just_the_first():
    citations = [cite(0, 1, "A", "a"), cite(1, 2, "B", "a"), cite(2, 3, "C", "bad")]
    assert validate(citations, {"a"}) is False


# --------------------------------------------------------------------------
# span_defects -- the check that fails on a real response
# --------------------------------------------------------------------------

def test_span_defects_is_empty_when_every_span_says_what_it_quotes():
    answer = "The system must be reviewed."
    assert span_defects(answer, [cite(4, 10, "system"), cite(0, 3, "The")]) == []


def test_span_defects_catches_a_span_that_quotes_different_words():
    """This is what a `content_index` rebasing error looks like from the outside:
    both offsets are in range and the text is real, but they do not correspond.
    Nothing raises."""
    answer = "Providers must keep logs. Deployers must inform workers."
    # The second block's citation, passed through without rebasing.
    assert span_defects(answer, [cite(0, 9, "Deployers")]) == [0]


def test_span_defects_catches_an_end_past_the_answer():
    assert span_defects("Short.", [cite(0, 999, "Short.")]) == [0]


def test_span_defects_catches_an_inverted_span():
    assert span_defects("Some answer.", [cite(9, 4, "answer")]) == [0]


def test_span_defects_catches_a_negative_start():
    assert span_defects("Some answer.", [cite(-1, 4, "Some")]) == [0]


def test_span_defects_returns_positions_so_the_artifact_can_name_them():
    answer = "Alpha beta gamma."
    citations = [cite(0, 5, "Alpha"), cite(6, 10, "WRONG"), cite(11, 16, "gamma")]
    assert span_defects(answer, citations) == [1]


def test_a_zero_length_span_is_not_a_defect_if_the_text_is_empty():
    """Degenerate but in range and self-consistent. Flagging it would put noise
    in a rate that is supposed to mean something."""
    assert span_defects("Answer.", [cite(3, 3, "")]) == []


def test_span_defects_on_an_empty_answer_flags_any_non_empty_citation():
    assert span_defects("", [cite(0, 5, "Alpha")]) == [0]


# --------------------------------------------------------------------------
# uncited_labels -- the one that will be non-zero
# --------------------------------------------------------------------------

def test_uncited_labels_catches_a_label_no_document_carried():
    """The model copying a provision out of a `+121 more` tail, or inventing an
    article number from its own knowledge of these two regulations."""
    answer = "The deployer must comply with AIA Art. 26(11) and AIA Art. 99(3)."
    assert uncited_labels(answer, {"AIA Art. 26(11)"}) == ["AIA Art. 99(3)"]


def test_uncited_labels_is_empty_when_every_label_was_shown():
    answer = "See AIA Art. 9(1) and GDPR Art. 83(5)."
    assert uncited_labels(answer, {"AIA Art. 9(1)", "GDPR Art. 83(5)", "AIA Art. 60"}) == []


def test_uncited_labels_dedupes_so_a_repeated_invention_is_one_finding():
    answer = "AIA Art. 5(1) says so. Again, AIA Art. 5(1). And AIA Art. 5(1)."
    assert uncited_labels(answer, set()) == ["AIA Art. 5(1)"]


def test_uncited_labels_preserves_first_appearance_order():
    answer = "GDPR Art. 9(2), then AIA Art. 6(3), then GDPR Art. 9(2) again."
    assert uncited_labels(answer, set()) == ["GDPR Art. 9(2)", "AIA Art. 6(3)"]


def test_uncited_labels_tolerates_the_model_spacing_a_label_differently():
    """`AIA Art.  9(1)` and `AIA Art. 9(1)` are the same citation and differ only
    in whitespace."""
    assert uncited_labels("As AIA Art.  9(1) states.", {"AIA Art. 9(1)"}) == []


def test_uncited_labels_finds_nothing_in_prose_that_names_no_provision():
    answer = "That question is outside the scope of both regulations."
    assert uncited_labels(answer, set()) == []


def test_an_annex_label_is_matched_as_a_whole_including_its_nesting():
    """`AIA Annex III(1)(a)` is one label. Matching only `AIA Annex III` would
    report a shown label as uncited, and matching `AIA Annex III(1` would report
    a real label under a string Phase 5 never grades."""
    assert uncited_labels("See AIA Annex III(1)(a).", {"AIA Annex III(1)(a)"}) == []
    assert uncited_labels("See AIA Annex III(1)(a).", {"AIA Annex III"}) == [
        "AIA Annex III(1)(a)"
    ]


def test_normalise_label_collapses_whitespace_and_changes_nothing_else():
    assert normalise_label("AIA  Art.\n9(1)") == "AIA Art. 9(1)"
    assert normalise_label("GDPR Art. 83(5)") == "GDPR Art. 83(5)"


# --------------------------------------------------------------------------
# LABEL_RE against the committed eval set
# --------------------------------------------------------------------------

def test_label_re_matches_every_distinct_label_form_in_the_eval_set(gold_labels):
    """THE CORRECTION TO THE PLAN'S REGEX, PINNED.

    The plan proposed `\\b(?:AIA|GDPR)\\s+(?:Art\\.|Annex)\\s*[^\\s,;)]+`, whose
    character class excludes `)` and therefore stops *inside* the parenthesis:
    it matches `AIA Art. 9(1` and never `AIA Art. 9(1)`. Every gold label except
    `AIA Art. 60` carries a parenthesised part, so that form would have
    mismatched 40 of these 41 strings -- by one character each, which is exactly
    the kind of defect a read-through does not catch.
    """
    assert len(gold_labels) == DISTINCT_GOLD_LABELS
    for label in sorted(gold_labels):
        assert LABEL_RE.fullmatch(label), f"{label!r} is not matched end to end"


def test_label_re_finds_labels_embedded_in_prose(gold_labels):
    """It has to work inside a sentence, which is where the model puts them."""
    for label in sorted(gold_labels):
        found = LABEL_RE.findall(f"The rule at {label} applies, and nothing else does.")
        assert found == [label], f"{label!r} -> {found}"


def test_label_re_matches_the_sectioned_annex_form_chunk_builds():
    """`Chunk.citation_label` nests the section for Annexes VIII and XI --
    `AIA Annex VIII(A)(1)` -- because without it 25 chunks share 11 labels. The
    eval set contains no such label today, so nothing else would catch a regex
    that could not read one."""
    assert LABEL_RE.fullmatch("AIA Annex VIII(A)(1)")
    assert LABEL_RE.fullmatch("AIA Annex XI(2)(1)")


def test_label_re_does_not_match_a_bare_article_number():
    """`Art. 9(1)` without a regulation is ambiguous between two regulations that
    both have an Article 9, and both are in this corpus."""
    assert LABEL_RE.findall("Art. 9(1) says so.") == []


def test_label_re_does_not_match_a_regulation_named_on_its_own():
    assert LABEL_RE.findall("The GDPR and the AIA both apply.") == []
