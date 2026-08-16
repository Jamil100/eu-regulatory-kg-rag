"""Decomposed synthesis: extraction per paragraph, deterministic assembly.

Pure -- no database, no key, no network. The client is a fake returning the
replies the real model was observed to return, including the malformed ones.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.answer.decompose import (
    DecomposeError,
    ParagraphRecord,
    _parse,
    assemble_records,
    decomposed_answer,
    extract_record,
)
from src.schemas import ContextDoc


def _doc(chunk_id: str, label: str, text: str = "some statutory text") -> ContextDoc:
    return ContextDoc(chunk_id=chunk_id, text=text, citation_label=label,
                      source="PASSAGE", score=None)


class FakeClient:
    """Returns the given replies in order, in Cohere's response shape."""

    def __init__(self, replies: list[str], finish: str = "COMPLETE"):
        self.replies = list(replies)
        self.finish = finish
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            message=SimpleNamespace(
                content=[SimpleNamespace(text=self.replies.pop(0), type="text")]
            ),
            finish_reason=self.finish,
            usage=SimpleNamespace(
                billed_units=SimpleNamespace(input_tokens=100, output_tokens=20)
            ),
        )


# --------------------------------------------------------------------------
# Parsing -- one repair, and no others
# --------------------------------------------------------------------------

def test_a_fenced_reply_is_read():
    got = _parse('```json\n{"relevant": true, "subject": "s", "value": "v"}\n```')
    assert got["subject"] == "s"


def test_a_trailing_comma_is_repaired():
    """`{"relevant": false,}` -- observed on 2 of 11 Article 99 extractions, both
    `finish_reason: COMPLETE`, so a syntax habit rather than a truncation.
    Discarding them would drop a paragraph the model read correctly."""
    assert _parse('{"relevant": false,}')["relevant"] is False


def test_anything_else_raises_rather_than_defaulting():
    """A record silently defaulted to `relevant: false` is indistinguishable from
    the model judging the paragraph irrelevant, and would drop a limb of the
    enumeration with no trace. Same reasoning as `judge._parse`."""
    for bad in ("not json at all", '{"subject": "s"}', '{"relevant": tru}'):
        with pytest.raises(DecomposeError):
            _parse(bad)


# --------------------------------------------------------------------------
# Assembly -- deterministic, and the spans are exact
# --------------------------------------------------------------------------

def test_the_spans_index_the_answer_exactly():
    """This path builds the answer string itself, so unlike the native-citation
    path there is nothing to rebase and `span_defects` has nothing to find."""
    records = [
        ParagraphRecord("c1", "AIA Art. 99(3)", True, "prohibited practices", "EUR 35 000 000"),
        ParagraphRecord("c2", "AIA Art. 99(4)", True, "operator obligations", "EUR 15 000 000"),
    ]
    answer, citations = assemble_records(records)
    assert len(citations) == 2
    for citation in citations:
        assert answer[citation.start:citation.end] == citation.text


def test_irrelevant_and_failed_records_contribute_nothing():
    records = [
        ParagraphRecord("c1", "L1", True, "s", "v"),
        ParagraphRecord("c2", "L2", False),
        ParagraphRecord("c3", "L3", True, "s", "", error="boom"),
    ]
    answer, citations = assemble_records(records)
    assert [c.chunk_id for c in citations] == ["c1"]
    assert "L2" not in answer and "L3" not in answer


def test_assembly_preserves_the_order_given():
    """Statutory order, because that is what `enumerate_provision` produced and
    the only ordering on this stratum correct by construction. Re-sorting here
    would put a ranking decision back into the path built to avoid one."""
    records = [
        ParagraphRecord(f"c{n}", f"L{n}", True, f"s{n}", f"v{n}") for n in (3, 1, 2)
    ]
    answer, _ = assemble_records(records)
    assert answer.index("L3") < answer.index("L1") < answer.index("L2")


def test_an_empty_record_set_gives_an_empty_answer():
    assert assemble_records([]) == ("", [])


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def test_a_relevant_paragraph_becomes_a_record():
    client = FakeClient(['{"relevant": true, "subject": "fines", "value": "EUR 35m"}'])
    record, _in, _out = extract_record("q", _doc("c1", "AIA Art. 99(3)"), client)
    assert (record.relevant, record.subject, record.value) == (True, "fines", "EUR 35m")
    assert record.error is None


def test_one_paragraph_failing_does_not_fail_the_row():
    """Fourteen paragraphs and one unreadable reply should cost that paragraph,
    not the answer -- the artifact then shows a short answer with a named cause
    instead of a missing row."""
    client = FakeClient(['garbage', '{"relevant": true, "subject": "s", "value": "v"}'])
    docs = [_doc("c1", "L1"), _doc("c2", "L2")]
    got = decomposed_answer("q", docs, client=client)
    assert got.calls == 2
    assert [c.chunk_id for c in got.citations] == ["c2"]
    assert any("c1" in d for d in got.dropped)


def test_relevant_with_no_value_is_downgraded():
    """`relevant: true` with an empty `value` would assemble into a sentence
    consisting only of a citation label."""
    client = FakeClient(['{"relevant": true, "subject": "s", "value": ""}'])
    record, _in, _out = extract_record("q", _doc("c1", "L1"), client)
    assert record.relevant is False
    assert record.error


def test_a_truncated_extraction_is_named_as_such():
    """A cap problem and a model problem are different facts, and the first
    read as a relevance judgement is how MAX_TOKENS deleted aggregation failures
    before `generate.MAX_TOKENS` went to 2000."""
    client = FakeClient(['{"relevant": true, "subject": "s", "val'], finish="MAX_TOKENS")
    record, _in, _out = extract_record("q", _doc("c1", "L1"), client)
    assert "TRUNCATED" in (record.error or "")


def test_the_extraction_sends_one_paragraph_and_no_documents():
    """THE WHOLE MECHANISM. A call that can see only Art. 99(4) cannot pair its
    subject with Art. 99(5)'s ceiling, because the other ceiling is not in the
    room. If a second paragraph ever reaches one call, this stops being
    decomposed synthesis and the arm measures nothing."""
    client = FakeClient(['{"relevant": false}'] * 2)
    decomposed_answer("q", [_doc("c1", "L1", "text one"), _doc("c2", "L2", "text two")],
                      client=client)
    assert len(client.calls) == 2
    for call, expected, absent in ((client.calls[0], "text one", "text two"),
                                   (client.calls[1], "text two", "text one")):
        user = [m for m in call["messages"] if m["role"] == "user"][0]["content"]
        assert expected in user and absent not in user
        assert not call.get("documents")


def test_no_documents_means_no_call_at_all():
    with pytest.raises(DecomposeError):
        decomposed_answer("q", [], client=FakeClient([]))
