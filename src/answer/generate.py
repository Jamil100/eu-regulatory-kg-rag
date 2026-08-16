"""Grounded answer generation with Command A + the `documents` parameter.

Returns native citation spans mapping answer text to source documents.

FOUR THINGS HERE WERE VERIFIED AGAINST THE INSTALLED `cohere==7.0.8` RATHER THAN
AGAINST THE DOCS, AND EACH IS SILENT WHEN WRONG.

**1. `documents` is `Union[str, Document]`, and `Document` requires `data: dict`.**
A bare `{"text": ...}` v1-style dict does not satisfy it. `context_assembly`
emits `{"id", "data"}` for this reason, and `data`'s keys are shown to the model.

**2. `start`/`end` index `content[content_index]`, not the answer.**
`message.content` is a discriminated union of `text` and `thinking` blocks, and a
citation reports its offsets into one block plus the index of that block. The
house idiom for reading a response is `"".join(item.text for item in
content)` (`template_selector.py:481`, `router.py:304`), which is correct for a
one-block answer and silently produces wrong offsets the moment there are two --
and `Citation.start`/`end` are declared as *answer* offsets (`schemas.py:280`).
So this module walks the content, records `content_index -> offset` for the text
blocks it kept, skips the rest, and rebases. A citation whose `content_index` is
absent from that map is dropped: it points into a block that is not in the
answer, and a rebased offset would be a confident lie.
`citation_validator.span_defects` is the check with teeth, and it is the one test
here that fails on a real response if this is wrong.

**3. `start`, `end` and `text` are all `Optional` on the wire and required on
`Citation`.** A `None` in any of them is a *dropped citation*, not a crash: the
answer is still correct and still has its other citations, and taking a request
path down over one malformed span would be the same category error
`RouterError`'s docstring names at `router.py:101`. Same for a `ToolSource` where
a `DocumentSource` was expected -- this call passes no tools, so one would mean
the response shape changed under us, which is a thing to record rather than to
raise on. Every drop is counted and named in `GenerationResult.dropped`.

**4. Cost reads `usage.billed_units`, with a fallback to `usage.tokens`.**
Billed units are what you are charged; `template_selector.py:481-488` already
reads them this way. The older `usage.tokens` dialect at `extract.py:411` and
`router.py:305` is the fallback rather than the primary, because the two differ
whenever a request hits the inference cache.

CITATION FAN-OUT IS TWO-LEVEL.

One Cohere citation carries N `DocumentSource`s; each of those resolves to one
assembled `ContextDoc`; and a GRAPH document carries up to `MAX_PROVENANCE`
chunks in `provenance`. So one span can produce several `Citation` rows sharing
`start`/`end`/`text` and differing in `chunk_id`. That is the correct shape -- a
statement asserted by three provisions is cited to three provisions -- and it is
capped at what the statement's own text *showed*, never at the full 124. Citing
121 chunks a reader was never shown is not evidence.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src.config import price_of, settings
from src.schemas import Citation, ContextDoc

__all__ = [
    "SYSTEM_PROMPT",
    "MAX_TOKENS",
    "GenerateError",
    "GenerationResult",
    "build_messages",
    "generate",
    "generate_detailed",
]

# Long enough for a two-part legal answer with a caveat, short enough that a
# model padding out an evasion truncates and is caught. `finish_reason` is
# carried either way -- see the note on MAX_TOKENS in `GenerationResult`.
MAX_TOKENS = 800

# THE PROMPT IS THE ONLY REFUSAL LEVER THIS PATH HAS.
#
# `query-path.md:459-467` measured rerank confidence as useless for the purpose:
# the cross-encoder returns 0.747 on `oos-001` and 0.796 on `oos-002` against
# 0.90 for a well-answered question, so no threshold separates an unanswerable
# question from a real one. There is no second signal. Everything the four
# refusal rows are graded on has to come from these words.
#
# The tension is stated here before the sweep rather than discovered in it:
# `oos-002` (unanswerable) says **any** citation is wrong, while `hn-001` and
# `hn-002` (hard-negative) say an uncited correct answer is only partial. One
# prompt has to produce both, from the same `vector` route, on adjacent
# questions. The instruction below tries to resolve it by keying on whether the
# documents *contain* the answer rather than on whether the question sounds
# answerable -- which is the only distinction available to a model that cannot
# see the eval set's `must_cite` flag.
SYSTEM_PROMPT = """\
You answer questions about the EU AI Act (Regulation (EU) 2024/1689) and the \
GDPR (Regulation (EU) 2016/679), using only the documents provided with the \
question.

Each document is one of two kinds. A document whose `source` is PASSAGE is \
verbatim legislative text. A document whose `source` is GRAPH is a relationship \
extracted from the legislation and rendered as a sentence, followed by the \
provisions that assert it; treat it as a pointer to those provisions, not as \
quotable statutory language.

Rules, in order of precedence:

1. Ground every substantive claim in the documents. Do not use knowledge of \
these regulations that is not in front of you, and do not fill a gap from \
memory.
2. If the documents do not contain the answer, say so plainly and name what is \
missing -- the specific provision, figure or rule you would need. Do not \
approximate, and do not cite a document that is merely on the same topic.
3. If the question is about something outside these two regulations, say that it \
is outside their scope and name the scope limit. Do not answer it from general \
knowledge.
4. If the documents contain the answer, give it directly, including the figure \
or the tier where the question asks for one, and cite the provisions it rests \
on.
5. If the question's premise is wrong -- it assumes a rule, a threshold or a \
regulation that does not apply -- correct the premise first, then give the rule \
that does apply.
6. Prefer the most specific provision. Where a general rule has an exception \
that the documents state, give both; a rule stated without its exception is \
legally misleading.

Be concise. Answer in prose, not in headings or bullet lists."""


class GenerateError(RuntimeError):
    """An answer could not be generated.

    Deliberately not `SystemExit`, for the reason recorded at
    src/query/router.py:101 -- this runs inside a FastAPI worker at Step 7.
    """


@dataclass
class GenerationResult:
    """One Command A call, with everything the artifact and Step 7 need.

    `finish_reason` is carried rather than checked-and-discarded. A `MAX_TOKENS`
    row is a truncated answer whose last citation may be truncated with it, so
    its span and label rates measure the token limit rather than the model --
    those rows are **excluded from the published rejection rate**, the same
    treatment `reranker.py:400` gives `attempts != 1`.

    `content_blocks` is recorded because it is the trigger for the rebasing bug
    in this module's docstring. If it is ever >1 on this eval set, the offsets in
    the artifact were produced by code that had to do something non-trivial, and
    that is worth being able to see.

    `dropped` names each citation this module refused to emit and why, so a
    silent zero and a silently-swallowed twelve look different in the artifact.

    `request_sha` is the SHA-256 of the exact body sent to `client.chat`. It is
    an instrument, not a control: nothing reads it at request time. It exists so
    that "the same system returned different text on two runs" can be separated
    into "the provider is nondeterministic" and "we sent a different request and
    did not notice" -- which are the same observation until the payload is
    hashed, and which have completely different fixes.
    """

    answer: str = ""
    citations: list[Citation] = field(default_factory=list)
    finish_reason: str = ""
    content_blocks: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float = 0.0
    attempts: int = 1
    dropped: list[str] = field(default_factory=list)
    request_sha: str = ""


def get_client() -> Any:
    """A Cohere client that raises `GenerateError` rather than exiting.

    Same split as `router.get_client()` (router.py:243) and
    `retriever.get_client()` (retriever.py:90): `embedder.get_client()` raises
    `SystemExit` on a missing key, which is right for a CLI and wrong here.
    """
    import cohere

    if not settings.cohere_api_key:
        raise GenerateError(f"{settings.cohere_api_key_var} is not set")
    return cohere.ClientV2(api_key=settings.cohere_api_key)


def build_messages(question: str) -> list[dict[str, str]]:
    """System prompt plus the question. No few-shot examples, deliberately.

    `router.FEW_SHOT` exists because a three-way classification needs its output
    format demonstrated. Here the output format is prose and the grounding comes
    from `documents`; a worked example would be a hand-written answer about these
    two regulations, which is a style the judge would then be grading instead of
    the model's. `router.py:18-27` records what leakage costs, and the cheapest
    way not to leak is to have nothing to leak.
    """
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]


def _chat_call(
    client: Any, messages: list[dict], documents: list[dict]
) -> tuple[Any, int, str]:
    """The retrying unit, mirroring `template_selector._chat_call`.

    **This is the third new chat call site in three steps, and the first two both
    shipped without a retry and were both bitten** -- Step 4's reranker died
    mid-sweep on a 429 from a 10-calls/minute trial key, and Step 5's selector
    scored `th-004` a zero for the same reason one step after that lesson was
    written down (`query-path.md`, Defects). Nothing in the repo enforces this,
    so it is written out again here rather than inherited.

    `attempts` is returned for the reason `reranker.py:120-133` gives: a call
    that retried spent most of its wall clock asleep in backoff, and averaging
    that into a latency percentile produces a number about the API key.
    """
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential_jitter,
    )

    from src.index.embedder import RETRYABLE_ERRORS

    # THE BODY IS BUILT ONCE AND IS BOTH HASHED AND SENT.
    #
    # `request_sha` exists to answer exactly one question, so it has to be beyond
    # dispute: when two runs of the same system return different text, was the
    # request the same? A hash taken over a *reconstruction* of the payload
    # cannot answer that -- it would prove only that the reconstruction matched.
    # So the dict below is the argument to `client.chat`, splatted at the call,
    # and the hash is taken over that same object. There is no second copy that
    # could drift from what went over the wire.
    body: dict[str, Any] = dict(
        model=settings.model_generate,
        messages=messages,
        documents=documents,
        # Explicit, not defaulted. `citation_options.mode` defaults to
        # "enabled" and Cohere may move what that resolves to; a default that
        # changes upstream silently re-measures this whole step. Same reason
        # `dim=512` is passed explicitly at every call site (retriever.py:20-24).
        #
        # `ENABLED`, NOT `ACCURATE` -- A CORRECTION TO THE PLAN, MADE BY THE
        # API. The plan specified `{"mode": "ACCURATE"}`, which is in the SDK
        # enum (`citation_options_mode.py` lists ENABLED/DISABLED/FAST/
        # ACCURATE/OFF) and which `command-a-03-2025` rejects with a 400:
        # *"This model does not support the provided citation mode:
        # accurate."* ACCURATE/FAST are the v1 Command R `citation_quality`
        # values; the enum is the union over every model and is not a
        # contract with any one of them. Probed live 2026-08-05 -- FAST,
        # ENABLED, OFF and DISABLED all succeed; ENABLED and FAST return
        # citations, OFF and DISABLED return none. ENABLED is pinned because
        # it is the model's full citation pass, which is what the plan was
        # asking ACCURATE for.
        citation_options={"mode": "ENABLED"},
        temperature=0,
        seed=42,
        max_tokens=MAX_TOKENS,
        # Deliberately absent:
        #   response_format -- JSON mode lands citation spans on braces and
        #     field names rather than on prose, which breaks `span_defects`.
        #   safety_mode -- not configurable in combination with `documents`
        #     (v2/client.py:271-277); passing it would be silently ignored.
        #   thinking -- a second content block for no benefit here, and the
        #     block-offset bug this module rebases around is exactly what it
        #     would trigger.
    )

    # Canonical: sorted keys, no ASCII escaping, so the digest is a property of
    # the payload rather than of dict ordering or of how json chose to escape a
    # section sign. `default=str` is a backstop -- everything here is already
    # JSON-native, and a future non-serialisable value should change the hash
    # rather than raise inside an instrument.
    request_sha = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()

    @retry(
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        wait=wait_exponential_jitter(initial=2, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def _call() -> Any:
        return client.chat(**body)

    response = _call()
    # `_call.statistics`, NOT `_call.retry.statistics`. The two existing call
    # sites in this repo read the second and it is **permanently empty**: since
    # tenacity 8.2.3 `wrapped_f` runs `copy = self.copy()` per invocation and
    # assigns the copy's statistics to `wrapped_f.statistics`, while
    # `wrapped_f.retry` stays the original controller that never runs. So
    # `.get("attempt_number", 1)` has been returning the default every time.
    # Verified against the installed tenacity 9.1.4. See
    # `tests/test_generate.py::test_the_retry_fires_...` and the failure-notes
    # entry -- this is "verified once by hand, never encoded" again, one layer
    # further down: the retry policy *was* encoded, and the instrument that
    # reports whether it fired was not.
    return response, int(_call.statistics.get("attempt_number", 1)), request_sha


def _text_blocks(content: Any) -> tuple[str, dict[int, int], int]:
    """The answer, the `content_index -> answer offset` map, and the block count.

    The map is what makes a citation's offsets mean what `schemas.py:280` says
    they mean. Only `text` blocks contribute, and a block that is not text does
    not shift the offsets of the ones after it -- it is not in the answer, so it
    occupies no characters of it.
    """
    blocks = list(content or [])
    parts: list[str] = []
    offsets: dict[int, int] = {}
    cursor = 0
    for index, block in enumerate(blocks):
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue  # a `thinking` block, or a future block type
        offsets[index] = cursor
        parts.append(text)
        cursor += len(text)
    return "".join(parts), offsets, len(blocks)


def _citations_from(
    response: Any,
    by_id: dict[str, ContextDoc],
    offsets: dict[int, int],
    answer: str,
    labels: Mapping[str, str],
) -> tuple[list[Citation], list[str]]:
    """Cohere's citations as `Citation` rows, rebased and fanned out.

    Returns the surviving rows and a reason string per drop. Every `continue`
    below is a citation the model produced that this path will not stand behind,
    and each one is named rather than counted.
    """
    out: list[Citation] = []
    dropped: list[str] = []
    raw = list(getattr(response.message, "citations", None) or [])

    for position, citation in enumerate(raw):
        start, end, text = citation.start, citation.end, citation.text
        if start is None or end is None or text is None:
            dropped.append(f"citation[{position}]: start/end/text is None")
            continue

        # `content_index` is Optional and defaults to the first block when the
        # response has only one -- but a missing index on a multi-block response
        # is not something to guess about.
        content_index = citation.content_index
        if content_index is None:
            content_index = next(iter(offsets), None) if len(offsets) == 1 else None
        if content_index not in offsets:
            dropped.append(
                f"citation[{position}]: content_index={citation.content_index} is not a "
                f"text block in the answer"
            )
            continue

        base = offsets[content_index]
        start, end = base + start, base + end
        if not (0 <= start <= end <= len(answer)):
            dropped.append(f"citation[{position}]: span [{start}:{end}] outside the answer")
            continue

        sources = list(citation.sources or [])
        if not sources:
            dropped.append(f"citation[{position}]: no sources")
            continue

        for source in sources:
            source_type = getattr(source, "type", None)
            if source_type != "document":
                # This call passes no tools, so a ToolSource means the response
                # shape moved. Recorded, not raised on.
                dropped.append(f"citation[{position}]: source type {source_type!r}")
                continue
            doc = by_id.get(getattr(source, "id", None) or "")
            if doc is None:
                # An id the model generated rather than echoed. This is one of
                # the four ways `validate()` can actually return False -- see its
                # docstring -- and it is caught here as well so the reason
                # survives into the artifact.
                dropped.append(
                    f"citation[{position}]: unknown document id {getattr(source, 'id', None)!r}"
                )
                continue

            # The fan-out. `provenance` is what the statement's own text showed,
            # so a graph statement citing three provisions produces three rows;
            # a passage carries `provenance == []` and produces one.
            for chunk_id in doc.provenance or [doc.chunk_id]:
                out.append(
                    Citation(
                        chunk_id=chunk_id,
                        start=start,
                        end=end,
                        text=text,
                        # The label of the chunk actually cited, which for a
                        # fanned-out graph citation is not the document's own
                        # label -- `provenance[1]` and `provenance[2]` name other
                        # provisions. `labels` is injected for the same reason
                        # `path_to_prose(labels=...)` is: `citation_label` is a
                        # SELECTed column, never recomputed, and deriving it here
                        # would be a second code path producing the string Phase
                        # 5 grades on (`retriever.py:170`, `graph_path.py:15-20`).
                        # Falls back to the document's own label, which is exact
                        # for a passage and for `provenance[0]`.
                        citation_label=labels.get(chunk_id) or (
                            doc.citation_label if chunk_id == doc.chunk_id else ""
                        ),
                        source=doc.source,
                        document_id=getattr(source, "id", "") or "",
                    )
                )
    return out, dropped


def generate_detailed(
    question: str,
    documents: list[dict],
    *,
    client: Any | None = None,
    by_id: dict[str, ContextDoc] | None = None,
    labels: Mapping[str, str] | None = None,
    messages: list[dict] | None = None,
) -> GenerationResult:
    """`generate()` plus the tokens, cost, latency and drop log Step 7 records.

    `by_id` is `AssemblyResult.by_id` and is what turns an echoed document id
    back into a chunk id. Without it there is nothing to map through and the
    citations come back empty -- which is a legitimate call (a caller that only
    wants the prose) rather than an error, so it is not required.

    `labels` is `chunk_id -> citation_label` for the fan-out, injected rather
    than looked up so this function stays testable with no database -- the same
    contract `path_to_prose(labels=...)` carries and for the same reason.

    `messages` is the injection point the regenerate-once loop uses: it appends a
    user turn naming the rejected ids and re-states the grounding rule. At
    `temperature=0, seed=42` an identical second request is a guaranteed second
    charge for a guaranteed identical failure, so the retry has to change
    something -- see `answer_path.answer()`.
    """
    if not documents:
        # No call at all. An answer grounded in nothing is not a refusal this
        # path is entitled to bill for, and the caller has the route that
        # produced zero documents.
        raise GenerateError("no documents to generate from")

    from cohere.core import ApiError

    client = client or get_client()
    started = time.perf_counter()
    try:
        response, attempts, request_sha = _chat_call(
            client, messages or build_messages(question), documents
        )
    except ApiError as exc:
        raise GenerateError(f"generation failed: {type(exc).__name__}: {exc}") from exc
    latency_ms = (time.perf_counter() - started) * 1000

    answer, offsets, blocks = _text_blocks(getattr(response.message, "content", None))
    citations, dropped = _citations_from(
        response, by_id or {}, offsets, answer, labels or {}
    )

    usage = getattr(response, "usage", None)
    billed = getattr(usage, "billed_units", None) if usage else None
    if billed is None and usage is not None:
        billed = getattr(usage, "tokens", None)  # the extract.py:411 dialect
    input_tokens = int(getattr(billed, "input_tokens", 0) or 0) if billed else 0
    output_tokens = int(getattr(billed, "output_tokens", 0) or 0) if billed else 0

    return GenerationResult(
        answer=answer,
        citations=citations,
        finish_reason=str(getattr(response, "finish_reason", "") or ""),
        content_blocks=blocks,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=price_of(settings.model_generate, input_tokens, output_tokens),
        latency_ms=latency_ms,
        attempts=attempts,
        dropped=dropped,
        request_sha=request_sha,
    )


def generate(question: str, documents: list[dict]) -> tuple[str, list[Citation]]:
    """Generate a grounded answer and native citations.

    The declared signature is honoured, but it **cannot return chunk-level
    citations** and that is a property of the signature rather than an oversight:
    a document dict holds `d0..dN` and no corpus key by design, so there is
    nothing here to map an echoed source id back through. Every citation is
    therefore dropped as "unknown document id" and `citations` comes back empty.

    Callers that need citations pass `generate_detailed(by_id=..., labels=...)`,
    which is what `answer_path` does. This form exists for a caller that wants
    the prose and for the declared interface not to change under Step 7.
    """
    result = generate_detailed(question, documents)
    return result.answer, result.citations
