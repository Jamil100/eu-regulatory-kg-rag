"""Decomposed synthesis: one extraction per paragraph, then deterministic assembly.

WHAT THIS IS FOR, AND THE ONE FAILURE IT TARGETS.

Enumeration fixed retrieval on the aggregation stratum -- gold in the prompt went
from 12/48 to 33/48 -- and the failure moved downstream rather than away.
`ag-006` is the clean case: all four gold paragraphs of Article 99 are in the
prompt, all four are cited, and the answer is **wrong on all three runs** because
it attaches the EUR 7.5M ceiling to the wrong infringement class. Its grading
rule is explicit that this is wrong rather than partial -- "pairing a ceiling
with the wrong subject matter is wrong".

That is a cross-paragraph confusion, and it is the same shape as the failure that
broke ranking (siblings displacing each other) and retrieval (enumerated points
collapsing under one embedding). Near-identical paragraphs differing only in
their operative value are hard to keep apart, and a single generation pass over
twelve of them is being asked to keep twelve (subject, value) pairs straight in
one shot.

THE FIX IS STRUCTURAL, NOT A BETTER PROMPT. Each paragraph is read on its own, in
its own call, with nothing else in context. A call that sees only Art. 99(4)
cannot pair its subject with Art. 99(5)'s ceiling -- the other ceiling is not in
the room. The answer is then assembled from the records by string concatenation,
with no model pass over the whole set, so there is no later opportunity to
re-introduce the confusion.

WHAT IT COSTS, STATED UP FRONT. This is N calls where there was 1, and N is the
enumerated article's length plus the ranked passages -- 14 on `ag-006`. It is
therefore only defensible if it actually flips the rows it was built for; if it
does not, the right move is to delete it rather than tune it. `--decompose` on
the benchmark exists to answer exactly that.

CITATIONS ARE EXACT HERE, WHICH IS A SIDE EFFECT WORTH NAMING. The normal path
takes Cohere's native citation spans and rebases them
(`generate.py`, "THE OFFSETS ARE REBASED"). This path builds the answer string
itself, so every span is computed from the string it just wrote and
`citation_validator.span_defects` cannot find anything to report. Nothing is
inferred and nothing is echoed back through the model.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.config import price_of, settings
from src.schemas import Citation

if TYPE_CHECKING:
    from src.schemas import ContextDoc

__all__ = [
    "DecomposeError",
    "DecompositionResult",
    "ParagraphRecord",
    "assemble_records",
    "decomposed_answer",
    "extract_record",
]


class DecomposeError(RuntimeError):
    """Decomposed synthesis could not produce an answer.

    Not `SystemExit`, for the reason `RouterError` gives at router.py:101.
    """


# Small on purpose -- the extraction returns two short strings and a boolean, and
# a generous cap would let a confused call write an essay into a field about to be
# concatenated into the answer.
#
# RAISED 220 -> 420 AFTER MEASURING. At 220, 2 of 11 Article 99 extractions
# truncated mid-JSON and were lost: the cap cut the reply inside the `value`
# string, which is unparseable and therefore indistinguishable from a refusal.
# `finish_reason` is now recorded per record so that failure mode is legible in
# the artifact rather than showing up as a paragraph the model "found
# irrelevant" -- which is the same class of defect as the MAX_TOKENS truncation
# that was deleting aggregation failures before `generate.MAX_TOKENS` went to
# 2000.
EXTRACT_MAX_TOKENS = 420

EXTRACT_SYSTEM = """You read ONE paragraph of EU legislation and report what it says, for a specific question.

Return a single JSON object and nothing else:
{"relevant": true|false, "subject": "...", "value": "..."}

relevant  true only if this paragraph is part of the answer to the question. A
          paragraph about a different matter is not relevant, however similar it
          looks. Scope, definitions and cross-references are usually not.
subject   what this paragraph governs, in six words or fewer, in the question's
          own terms. Not a restatement of the whole paragraph.
value     the operative content: the obligation, the ceiling, the right, the
          condition. Quote figures and percentages exactly as written. One
          sentence.

Report ONLY what this paragraph says. You cannot see the other paragraphs and
must not guess what they contain. If the paragraph does not answer the question,
say relevant false and leave the other fields empty."""


@dataclass
class ParagraphRecord:
    """One paragraph, read on its own."""

    chunk_id: str
    citation_label: str
    relevant: bool = False
    subject: str = ""
    value: str = ""
    error: str | None = None
    finish_reason: str = ""


@dataclass
class DecompositionResult:
    answer: str = ""
    citations: list[Citation] = field(default_factory=list)
    records: list[ParagraphRecord] = field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float = 0.0
    dropped: list[str] = field(default_factory=list)


_JSON = re.compile(r"\{.*\}", re.S)

# `{"relevant": false,}` -- a trailing comma before the closing brace, which is
# invalid JSON and which command-a emits on a minority of the "not relevant"
# replies (2 of 11 on Article 99, both `finish_reason: COMPLETE`, so not a
# truncation). Repaired rather than rejected because it is a syntax habit and
# not a wrong answer: the alternative is discarding a paragraph the model read
# correctly, which shows up downstream as a missing limb of the enumeration.
#
# This is the ONLY repair applied. Anything else still raises -- see `_parse`.
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _parse(raw: str) -> dict:
    """The model's JSON, or a raise.

    Fenced output is tolerated because it is a formatting habit rather than a
    wrong answer; anything else raises rather than being coerced, for the reason
    `judge._parse` gives -- a record silently defaulted to `relevant: false`
    would drop a paragraph from the answer and look like the model deciding it
    was irrelevant.
    """
    match = _JSON.search(raw or "")
    if not match:
        raise DecomposeError(f"no JSON object in extraction reply: {raw[:120]!r}")
    try:
        got = json.loads(_TRAILING_COMMA.sub(r"\1", match.group(0)))
    except json.JSONDecodeError as exc:
        raise DecomposeError(f"unparseable extraction reply: {raw[:120]!r}") from exc
    if not isinstance(got, dict) or "relevant" not in got:
        raise DecomposeError(f"extraction reply has no `relevant`: {raw[:120]!r}")
    return got


def extract_record(
    question: str, doc: ContextDoc, client: Any | None = None
) -> tuple[ParagraphRecord, int, int]:
    """Read one paragraph for one question. Returns the record and token counts.

    A failed extraction becomes a record carrying `error` rather than an
    exception that kills the row. One unreadable paragraph out of fourteen should
    cost that paragraph, not the answer -- and the artifact then shows a short
    answer with a named cause instead of a missing row.
    """
    from src.answer.generate import _chat_call, get_client

    client = client or get_client()
    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Paragraph {doc.citation_label}:\n{doc.text}"
            ),
        },
    ]
    record = ParagraphRecord(chunk_id=doc.chunk_id, citation_label=doc.citation_label)
    try:
        response, _attempts, _sha = _chat_call(
            client, messages, documents=[], max_tokens=EXTRACT_MAX_TOKENS
        )
    except Exception as exc:  # noqa: BLE001 -- one paragraph failing is not the row failing
        record.error = f"{type(exc).__name__}: {exc}"
        return record, 0, 0

    record.finish_reason = str(getattr(response, "finish_reason", "") or "")
    text = "".join(
        block.text for block in (response.message.content or []) if getattr(block, "text", None)
    )
    usage = getattr(response, "usage", None)
    billed = getattr(usage, "billed_units", None) if usage else None
    input_tokens = int(getattr(billed, "input_tokens", 0) or 0) if billed else 0
    output_tokens = int(getattr(billed, "output_tokens", 0) or 0) if billed else 0

    try:
        got = _parse(text)
    except DecomposeError as exc:
        # A truncated reply and a malformed one are different facts: the first is
        # this module's cap being too low, the second is the model. Naming which
        # is what stops a cap problem being read as a relevance judgement.
        cause = "TRUNCATED at EXTRACT_MAX_TOKENS: " if record.finish_reason == "MAX_TOKENS" else ""
        record.error = f"{cause}{exc}"
        return record, input_tokens, output_tokens

    record.relevant = bool(got.get("relevant"))
    record.subject = str(got.get("subject") or "").strip()
    record.value = str(got.get("value") or "").strip()
    if record.relevant and not record.value:
        record.relevant = False
        record.error = "relevant with no value"
    return record, input_tokens, output_tokens


def assemble_records(records: list[ParagraphRecord]) -> tuple[str, list[Citation]]:
    """Build the answer string from the records. No model call, no reordering.

    ORDER IS THE ORDER GIVEN, which is statutory order, because that is what
    `enumerate_provision` produced and the only ordering on this stratum that is
    correct by construction. Sorting by anything else here would re-introduce a
    ranking decision into the one path built to avoid one.

    Each relevant record contributes one sentence, `"<subject>: <value>
    (<label>)."`, and one citation whose span is computed from the string being
    built. The spans are therefore exact rather than rebased.
    """
    parts: list[str] = []
    citations: list[Citation] = []
    cursor = 0
    for record in records:
        if not record.relevant or not record.value:
            continue
        subject = record.subject.rstrip(" .:")
        value = record.value.strip()
        sentence = f"{subject}: {value}" if subject else value
        if not sentence.endswith("."):
            sentence += "."
        sentence = f"{sentence} ({record.citation_label})"
        start = cursor + (1 if parts else 0)
        parts.append(sentence)
        cursor = start + len(sentence)
        citations.append(
            Citation(
                chunk_id=record.chunk_id,
                start=start,
                end=cursor,
                text=sentence,
                citation_label=record.citation_label,
                source="PASSAGE",
            )
        )
    return " ".join(parts), citations


def decomposed_answer(
    question: str,
    docs: list[ContextDoc],
    *,
    client: Any | None = None,
) -> DecompositionResult:
    """One extraction call per document, then deterministic assembly.

    Sequential rather than concurrent, matching every other call site in this
    repo: the trial key that rate-limited the reranker mid-sweep and scored
    `th-004` a zero is the same key, and firing fourteen concurrent chat calls at
    it is the reliable way to reproduce both incidents at once.
    """
    if not docs:
        raise DecomposeError("decomposed synthesis needs at least one document")

    started = time.perf_counter()
    records: list[ParagraphRecord] = []
    input_tokens = output_tokens = 0
    for doc in docs:
        record, got_in, got_out = extract_record(question, doc, client)
        records.append(record)
        input_tokens += got_in
        output_tokens += got_out

    answer, citations = assemble_records(records)
    return DecompositionResult(
        answer=answer,
        citations=citations,
        records=records,
        calls=len(docs),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=price_of(settings.model_generate, input_tokens, output_tokens),
        latency_ms=(time.perf_counter() - started) * 1000,
        dropped=[
            f"{r.chunk_id}: {r.error}" for r in records if r.error
        ] + [
            f"{r.chunk_id}: not relevant" for r in records if not r.relevant and not r.error
        ],
    )
