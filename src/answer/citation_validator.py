"""Citation validation.

Assert every cited document ID was actually in the retrieved set. On failure,
regenerate once, then fail loudly. Count events for the README rejection rate.

`validate()` AS DECLARED CANNOT RETURN FALSE ON THE HAPPY PATH, AND SAYING SO IS
THE POINT OF THIS MODULE.

`generate()` maps `sources[].id` through the reverse map of the documents it just
sent, and `retrieved_ids` is those same documents' chunk ids. Membership then
holds *by construction*, and publishing the resulting `0%` as a rejection rate
would be `docs/failure-notes.md`'s "A metric that looked like success" row for a
fourth time -- alongside 0% pilot validation failure and 0 orphan reports from
nothing looking for orphans. The phase plan anticipates this
(`plan-phase-3-router-and-query-path.md:559-561`).

So the signature stays -- it is an ADR-0002-style boundary, it costs nothing, and
the four ways it *can* fire are real (see its docstring) -- and two checks with
teeth are added beside it:

  `span_defects`   fails on a real response. It is what catches the
                   `content_index` rebasing bug in `generate.py`: Cohere reports
                   offsets into one content block and `Citation.start`/`end` are
                   declared as offsets into the answer, so a two-block response
                   silently produces spans that point at the wrong words.
                   Nothing raises; `answer[start:end] != text` is the only
                   evidence.

  `uncited_labels` is the one that will be non-zero and the one Phase 5 cares
                   about. `eval-questions.jsonl` grades on label strings and
                   every `grading_rule` requires citation to a *retrieved*
                   chunk, so a model that writes "AIA Art. 26(11)" in prose --
                   copied out of a `+121 more` tail it was shown, or invented --
                   has produced an answer that reads as cited and is not.

All three rates are reported against the same denominator, or they cannot be
compared to each other.
"""

from __future__ import annotations

import re

from src.schemas import Citation

__all__ = ["LABEL_RE", "validate", "span_defects", "uncited_labels", "normalise_label"]

# Every citation label form the corpus and the eval set actually use:
#
#   AIA Art. 9(1)        AIA Art. 60          AIA Annex III(4)
#   GDPR Art. 83(5)      AIA Art. 3(26)       AIA Annex III(1)(a)
#   AIA Annex VIII(A)(1) -- the sectioned form `Chunk.citation_label` builds
#
# THIS IS A CORRECTION TO THE PLAN'S REGEX, recorded rather than silently
# applied. The plan proposed `\b(?:AIA|GDPR)\s+(?:Art\.|Annex)\s*[^\s,;)]+`,
# whose `[^\s,;)]+` excludes `)` and therefore stops **inside** the parenthesis:
# it matches `AIA Art. 9(1` and never `AIA Art. 9(1)`. Every label in
# `eval/eval-questions.jsonl` except `AIA Art. 60` carries a parenthesised part,
# so the proposed form would have mismatched 40 of the 41 distinct gold labels
# against the strings Phase 5 grades on -- and mismatched them *by one
# character*, which is the kind of defect that survives a read-through.
# `tests/test_citation_validator.py` asserts against the committed eval set, so
# this cannot regress to something that looks right.
LABEL_RE = re.compile(
    r"\b(?:AIA|GDPR)\s+(?:Art\.\s*\d+|Annex\s+[IVXLC]+)(?:\([A-Za-z0-9]{1,4}\))*"
)


def normalise_label(label: str) -> str:
    """Collapse internal whitespace so two spellings of one label compare equal.

    `AIA Art.  9(1)` and `AIA Art. 9(1)` are the same citation and differ only in
    how the model spaced it. Nothing else is normalised -- case is meaningful
    (`AIA` and `GDPR` are the regulation keys) and the parenthesised part is the
    provision.
    """
    return " ".join(label.split())


def validate(citations: list[Citation], retrieved_ids: set[str]) -> bool:
    """Return True iff every cited chunk_id is in the retrieved set.

    **On the happy path this cannot return False, and that is by construction
    rather than by luck.** `generate()` only emits a `Citation` for a source id
    it found in `AssemblyResult.by_id`, and `retrieved_ids` is
    `AssemblyResult.chunk_ids` -- the same documents' chunk ids and provenance.
    A caller who reads a 0% rejection rate off this function alone has measured
    the plumbing.

    The four ways it fires for real, each of which is a defect somewhere else:

    1. **A `ToolSource` where a `DocumentSource` was expected.** This call passes
       no tools, so one would mean the response shape moved under us.
       `generate._citations_from` drops it and records the reason, so it reaches
       here only if that guard is removed or a second producer of `Citation`
       appears.
    2. **An id the model generated rather than echoed.** Command A is asked to
       cite `d0..dN`; nothing stops it emitting `d99`. Caught in `generate` today
       and checked again here, because the two guards protect different
       consumers -- Step 7's `AskResponse` is built from `Citation`, not from
       Cohere's response.
    3. **A duplicate id collapsing the reverse map.** `by_id` is a dict keyed on
       `d0..dN`; a bug that emitted the same id twice would silently make one
       document unreachable and point its citations at the other's chunk.
    4. **A citation surviving a regeneration against a rebuilt document list.**
       The regenerate-once loop reassembles; if a future version reuses citations
       across the two calls, the ids no longer refer to the same documents. This
       is the one that is a live risk rather than a guard, because
       `answer_path.answer()` is where both lists exist at once.

    The rate that gets published is computed by `answer_path.scoreboard()` from
    the artifact, not by a counter in this module: a process-global counter is
    wrong in a FastAPI worker with more than one request in flight, and it is
    unauditable in every process.
    """
    return all(citation.chunk_id in retrieved_ids for citation in citations)


def span_defects(answer: str, citations: list[Citation]) -> list[int]:
    """Indices of citations whose span does not say what they claim it says.

    Two defects, both silent:

    * `answer[start:end] != text` -- the span points at different words than the
      citation quotes. This is what a `content_index` rebasing error looks like
      from the outside, and it is the reason `generate._text_blocks` exists.
    * the span is out of range or inverted -- `end` past the answer, or
      `start > end`. `Citation` declares both as `int` and nothing in Pydantic
      relates them to a string it has never seen.

    Returns positions rather than a bool so a caller can name the offending
    citation in an artifact. Empty means clean.
    """
    defects: list[int] = []
    for index, citation in enumerate(citations):
        if not (0 <= citation.start <= citation.end <= len(answer)):
            defects.append(index)
            continue
        if answer[citation.start : citation.end] != citation.text:
            defects.append(index)
    return defects


def uncited_labels(answer: str, labels_sent: set[str]) -> list[str]:
    """Provision labels the prose names that no document in the prompt carried.

    Two ways this goes non-zero, and they are not the same finding:

    * The model copied a label out of a statement's `+121 more` tail. The
      statement text names three provisions and says how many it withheld, so a
      model that names a fourth has read the count and invented a number -- but
      the label may still be a real provision.
    * The model invented an article number from its own knowledge of these two
      regulations. Rule 1 of `SYSTEM_PROMPT` forbids exactly this, and this is
      the check that measures whether the prompt worked.

    Either way the answer reads as cited and is not, and `eval-questions.jsonl`
    grades on these strings. Deduped, in first-appearance order, so a label
    repeated four times is one finding.
    """
    wanted = {normalise_label(label) for label in labels_sent}
    found: list[str] = []
    for match in LABEL_RE.finditer(answer):
        label = normalise_label(match.group(0))
        if label not in wanted and label not in found:
            found.append(label)
    return found
