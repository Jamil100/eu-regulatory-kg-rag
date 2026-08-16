"""Grounded generation, and the four things `cohere==7.0.8` is silent about.

No container, no API key, no spend. The fakes follow `FakeRerankClient` and
`ExplodingClient` in `tests/test_reranker.py`: a client that returns a response
in Cohere's shape, and one that asserts it was never called.

THE DEFECT THIS FILE EXISTS FOR IS THE OFFSET REBASING.

`Citation.start`/`end` are declared in `schemas.py:280` as offsets into the
answer. Cohere reports them as offsets into `content[content_index]`, and
`message.content` is a discriminated union of `text` and `thinking` blocks. The
house idiom for reading a response -- `"".join(item.text for item in content)`
at `template_selector.py:481` and `router.py:304` -- is correct for one block and
silently wrong for two: the second block's citations then point into the first
block's words. Nothing raises. `test_two_text_blocks_have_their_offsets_rebased`
is the pin.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.answer.citation_validator import span_defects
from src.answer.generate import (
    MAX_TOKENS,
    SYSTEM_PROMPT,
    GenerateError,
    build_messages,
    generate_detailed,
)
from src.schemas import ContextDoc

# `MAX_PROVENANCE` in path_to_prose -- how many provisions a statement names
# inline, and therefore the cap on the fan-out.
MAX_PROVENANCE = 3


# --------------------------------------------------------------------------
# Fakes -- Cohere's response shape, built by hand
# --------------------------------------------------------------------------

def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def thinking_block(text: str) -> SimpleNamespace:
    # No `text` attribute at all, which is what the real
    # ThinkingAssistantMessageResponseContentItem looks like.
    return SimpleNamespace(type="thinking", thinking=text)


def doc_source(doc_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="document", id=doc_id, document={})


def tool_source(source_id: str = "t0") -> SimpleNamespace:
    return SimpleNamespace(type="tool", id=source_id, tool_output={})


def citation(
    start: int | None,
    end: int | None,
    text: str | None,
    sources: list,
    content_index: int | None = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        start=start, end=end, text=text, sources=sources,
        content_index=content_index, type="TEXT_CONTENT",
    )


class FakeChatClient:
    """Returns the response it was constructed with, in Cohere's shape."""

    def __init__(
        self,
        blocks: list,
        citations: list | None = None,
        finish_reason: str = "COMPLETE",
        input_tokens: float = 1000,
        output_tokens: float = 200,
        billed: bool = True,
        token_dialect: bool = False,
    ):
        self.blocks = blocks
        self.citations = citations or []
        self.finish_reason = finish_reason
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.billed = billed
        self.token_dialect = token_dialect
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        counts = SimpleNamespace(
            input_tokens=self.input_tokens, output_tokens=self.output_tokens
        )
        usage = SimpleNamespace(
            billed_units=None if not self.billed else counts,
            tokens=counts if (self.token_dialect or not self.billed) else None,
        )
        return SimpleNamespace(
            message=SimpleNamespace(content=self.blocks, citations=self.citations),
            finish_reason=self.finish_reason,
            usage=usage,
        )


class ExplodingChatClient:
    def chat(self, **kwargs):
        raise AssertionError("the API was called when it should not have been")


def doc(chunk_id: str, source: str = "PASSAGE", provenance: list[str] | None = None) -> ContextDoc:
    return ContextDoc(
        chunk_id=chunk_id,
        text=f"text of {chunk_id}",
        citation_label=f"AIA Art. {chunk_id}",
        source=source,  # type: ignore[arg-type]
        provenance=provenance if provenance is not None else ([] if source == "PASSAGE" else [chunk_id]),
    )


DOCUMENTS = [{"id": "d0", "data": {"text": "t", "source": "PASSAGE", "citation": "L"}}]


# --------------------------------------------------------------------------
# Offsets -- the silent defect
# --------------------------------------------------------------------------

def test_one_text_block_maps_citations_to_the_right_chunk_ids():
    answer = "The system must be continuously reviewed."
    client = FakeChatClient(
        [text_block(answer)],
        [citation(4, 10, "system", [doc_source("d0")], content_index=0)],
    )
    result = generate_detailed(
        "q", DOCUMENTS, client=client, by_id={"d0": doc("aia-art9-para2")}
    )
    assert result.answer == answer
    assert [c.chunk_id for c in result.citations] == ["aia-art9-para2"]
    assert result.answer[result.citations[0].start : result.citations[0].end] == "system"


def test_two_text_blocks_have_their_offsets_rebased_into_the_joined_answer():
    """THE PIN. Cohere's offsets index `content[content_index]`; ours index the
    answer. Joining the blocks and passing the offsets through unchanged makes
    the second block's citations quote the first block's words -- and nothing
    raises, because both spans are in range."""
    first, second = "Providers must keep logs. ", "Deployers must inform workers."
    client = FakeChatClient(
        [text_block(first), text_block(second)],
        [
            citation(0, 9, "Providers", [doc_source("d0")], content_index=0),
            citation(0, 9, "Deployers", [doc_source("d1")], content_index=1),
        ],
    )
    result = generate_detailed(
        "q", DOCUMENTS, client=client,
        by_id={"d0": doc("a"), "d1": doc("b")},
    )
    assert result.answer == first + second
    assert result.content_blocks == 2
    assert span_defects(result.answer, result.citations) == []
    second_citation = next(c for c in result.citations if c.chunk_id == "b")
    assert second_citation.start == len(first)


def test_a_thinking_block_is_excluded_from_the_answer_and_from_the_offsets():
    """It is not in the answer, so it occupies none of its characters and must
    not shift the offsets of the text blocks after it."""
    client = FakeChatClient(
        [thinking_block("I should check Art. 9."), text_block("It is continuous.")],
        [citation(0, 2, "It", [doc_source("d0")], content_index=1)],
    )
    result = generate_detailed("q", DOCUMENTS, client=client, by_id={"d0": doc("a")})
    assert result.answer == "It is continuous."
    assert "I should check" not in result.answer
    assert span_defects(result.answer, result.citations) == []
    assert result.citations[0].start == 0


def test_a_citation_pointing_at_a_block_that_is_not_in_the_answer_is_dropped():
    """A rebased offset for a block the answer does not contain would be a
    confident lie."""
    client = FakeChatClient(
        [thinking_block("reasoning"), text_block("The answer.")],
        [citation(0, 9, "reasoning", [doc_source("d0")], content_index=0)],
    )
    result = generate_detailed("q", DOCUMENTS, client=client, by_id={"d0": doc("a")})
    assert result.citations == []
    assert any("content_index" in reason for reason in result.dropped)


def test_a_span_running_past_the_answer_is_dropped_not_clamped():
    client = FakeChatClient(
        [text_block("Short.")],
        [citation(0, 999, "Short.", [doc_source("d0")], content_index=0)],
    )
    result = generate_detailed("q", DOCUMENTS, client=client, by_id={"d0": doc("a")})
    assert result.citations == []
    assert any("outside the answer" in reason for reason in result.dropped)


# --------------------------------------------------------------------------
# The fan-out
# --------------------------------------------------------------------------

def test_one_citation_with_two_document_sources_fans_out_sharing_the_span():
    answer = "Both provisions apply."
    client = FakeChatClient(
        [text_block(answer)],
        [citation(0, 4, "Both", [doc_source("d0"), doc_source("d1")], content_index=0)],
    )
    result = generate_detailed(
        "q", DOCUMENTS, client=client, by_id={"d0": doc("a"), "d1": doc("b")}
    )
    assert [c.chunk_id for c in result.citations] == ["a", "b"]
    assert {(c.start, c.end, c.text) for c in result.citations} == {(0, 4, "Both")}


def test_a_graph_doc_with_three_provenance_entries_fans_out_to_three_citations():
    """A statement asserted by three provisions is cited to three provisions.
    Capped at what was *shown* -- `MAX_PROVENANCE` -- never at the full 124."""
    answer = "The duty applies."
    client = FakeChatClient(
        [text_block(answer)],
        [citation(0, 3, "The", [doc_source("d0")], content_index=0)],
    )
    graph = doc("aia-art26-para1", "GRAPH", ["aia-art26-para1", "aia-art26-para10", "aia-art26-para11"])
    labels = {
        "aia-art26-para1": "AIA Art. 26(1)",
        "aia-art26-para10": "AIA Art. 26(10)",
        "aia-art26-para11": "AIA Art. 26(11)",
    }
    result = generate_detailed(
        "q", DOCUMENTS, client=client, by_id={"d0": graph}, labels=labels
    )
    assert len(result.citations) == MAX_PROVENANCE
    assert {c.citation_label for c in result.citations} == set(labels.values())
    assert all(c.source == "GRAPH" for c in result.citations)
    assert all(c.document_id == "d0" for c in result.citations)


def test_a_passage_with_no_provenance_produces_exactly_one_citation():
    client = FakeChatClient(
        [text_block("One.")],
        [citation(0, 3, "One", [doc_source("d0")], content_index=0)],
    )
    result = generate_detailed("q", DOCUMENTS, client=client, by_id={"d0": doc("p")})
    assert len(result.citations) == 1
    assert result.citations[0].source == "PASSAGE"


def test_the_fan_out_label_comes_from_the_injected_map_not_from_the_prose():
    """`citation_label` is a SELECTed column, never recomputed. Parsing it back
    out of the statement text would be a second code path producing the string
    Phase 5 grades on."""
    client = FakeChatClient(
        [text_block("Yes.")],
        [citation(0, 3, "Yes", [doc_source("d0")], content_index=0)],
    )
    graph = doc("c1", "GRAPH", ["c1", "c2"])
    result = generate_detailed(
        "q", DOCUMENTS, client=client, by_id={"d0": graph},
        labels={"c1": "AIA Art. 5(1)", "c2": "AIA Art. 5(2)"},
    )
    assert sorted(c.citation_label for c in result.citations) == [
        "AIA Art. 5(1)", "AIA Art. 5(2)"
    ]


# --------------------------------------------------------------------------
# Drops, not crashes
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("start", "end", "text"),
    [(None, 4, "Both"), (0, None, "Both"), (0, 4, None)],
)
def test_a_none_in_start_end_or_text_is_a_dropped_citation_not_a_crash(start, end, text):
    """All three are Optional on the wire and required on `Citation`. The answer
    is still correct and still has its other citations."""
    client = FakeChatClient(
        [text_block("Both apply.")],
        [citation(start, end, text, [doc_source("d0")], content_index=0)],
    )
    result = generate_detailed("q", DOCUMENTS, client=client, by_id={"d0": doc("a")})
    assert result.answer == "Both apply."
    assert result.citations == []
    assert len(result.dropped) == 1


def test_a_tool_source_is_dropped_and_named():
    """This call passes no tools, so one would mean the response shape moved."""
    client = FakeChatClient(
        [text_block("Answer.")],
        [citation(0, 6, "Answer", [tool_source()], content_index=0)],
    )
    result = generate_detailed("q", DOCUMENTS, client=client, by_id={"d0": doc("a")})
    assert result.citations == []
    assert any("'tool'" in reason for reason in result.dropped)


def test_an_id_the_model_generated_rather_than_echoed_is_dropped():
    """One of the four ways `validate()` can actually fire. Caught here too, so
    the reason survives into the artifact rather than arriving as a bare False."""
    client = FakeChatClient(
        [text_block("Answer.")],
        [citation(0, 6, "Answer", [doc_source("d99")], content_index=0)],
    )
    result = generate_detailed("q", DOCUMENTS, client=client, by_id={"d0": doc("a")})
    assert result.citations == []
    assert any("unknown document id" in reason for reason in result.dropped)


def test_a_citation_with_no_sources_is_dropped():
    client = FakeChatClient(
        [text_block("Answer.")],
        [citation(0, 6, "Answer", [], content_index=0)],
    )
    result = generate_detailed("q", DOCUMENTS, client=client, by_id={"d0": doc("a")})
    assert result.citations == []


def test_a_response_with_no_citations_at_all_is_an_answer_not_an_error():
    """`oos-001` and `oos-002` are graded on producing exactly this."""
    client = FakeChatClient([text_block("That is outside the scope of both.")], [])
    result = generate_detailed("q", DOCUMENTS, client=client, by_id={"d0": doc("a")})
    assert result.answer.startswith("That is outside")
    assert result.citations == []
    assert result.dropped == []


# --------------------------------------------------------------------------
# The call itself
# --------------------------------------------------------------------------

def test_max_tokens_is_carried_not_swallowed():
    """A truncated answer can lose the second half of its last citation, so its
    span and label rates measure the token limit rather than the model. Those
    rows are excluded from the published rejection rate -- which needs them to be
    identifiable."""
    client = FakeChatClient([text_block("The answer is trunc")], [], finish_reason="MAX_TOKENS")
    result = generate_detailed("q", DOCUMENTS, client=client)
    assert result.finish_reason == "MAX_TOKENS"


def test_the_api_is_never_called_with_no_documents():
    """An answer grounded in nothing is not a refusal this path may bill for."""
    with pytest.raises(GenerateError, match="no documents"):
        generate_detailed("q", [], client=ExplodingChatClient())


def test_cost_reads_billed_units():
    """Billed units are what you are charged (template_selector.py:481-488)."""
    client = FakeChatClient([text_block("A.")], [], input_tokens=1000, output_tokens=100)
    result = generate_detailed("q", DOCUMENTS, client=client)
    assert result.input_tokens == 1000 and result.output_tokens == 100
    assert result.cost_usd == pytest.approx(1000 * 2.50e-6 + 100 * 10.00e-6)


def test_cost_falls_back_to_the_usage_tokens_dialect():
    """The older shape at extract.py:411 and router.py:305. The two differ
    whenever a request hits the inference cache, which is why billed is first."""
    client = FakeChatClient(
        [text_block("A.")], [], input_tokens=500, output_tokens=50, billed=False
    )
    result = generate_detailed("q", DOCUMENTS, client=client)
    assert result.input_tokens == 500 and result.output_tokens == 50


def test_the_call_pins_a_citation_mode_the_model_actually_accepts():
    """A default that changes upstream silently re-measures this whole step --
    the reason `dim=512` is passed explicitly at every call site.

    The mode is `ENABLED` and not the plan's `ACCURATE`. `ACCURATE` is in the SDK
    enum and `command-a-03-2025` returns a 400 for it: the enum is the union over
    every Cohere model, not a contract with any one of them. Probed live
    2026-08-05; ENABLED, FAST, OFF and DISABLED are accepted, and only the first
    two return citations at all -- so a mode chosen off the enum could also have
    silently produced an uncitable answer instead of an error.
    """
    client = FakeChatClient([text_block("A.")], [])
    generate_detailed("q", DOCUMENTS, client=client)
    assert client.calls[0]["citation_options"] == {"mode": "ENABLED"}
    assert client.calls[0]["citation_options"]["mode"] not in ("OFF", "DISABLED")


def test_the_call_is_deterministic_and_bounded():
    client = FakeChatClient([text_block("A.")], [])
    generate_detailed("q", DOCUMENTS, client=client)
    call = client.calls[0]
    assert call["temperature"] == 0 and call["seed"] == 42
    assert call["max_tokens"] == MAX_TOKENS


def test_the_call_passes_no_response_format_no_safety_mode_and_no_thinking():
    """JSON mode lands spans on braces; `safety_mode` is not configurable with
    `documents` (v2/client.py:271-277); `thinking` is a second content block for
    no benefit and triggers the offset rebasing this module works around."""
    client = FakeChatClient([text_block("A.")], [])
    generate_detailed("q", DOCUMENTS, client=client)
    assert not {"response_format", "safety_mode", "thinking"} & set(client.calls[0])


def test_the_call_asks_for_the_configured_generation_model():
    from src.config import settings

    client = FakeChatClient([text_block("A.")], [])
    generate_detailed("q", DOCUMENTS, client=client)
    assert client.calls[0]["model"] == settings.model_generate


def test_the_documents_reach_the_api_unchanged():
    client = FakeChatClient([text_block("A.")], [])
    generate_detailed("q", DOCUMENTS, client=client)
    assert client.calls[0]["documents"] == DOCUMENTS


def test_injected_messages_replace_the_default_turn_for_the_regeneration():
    """The regenerate-once loop has to change the request. At temperature 0 with
    a fixed seed, an identical second call is a second charge for the same
    failure."""
    client = FakeChatClient([text_block("A.")], [])
    messages = [*build_messages("q"), {"role": "user", "content": "you cited d99"}]
    generate_detailed("q", DOCUMENTS, client=client, messages=messages)
    assert client.calls[0]["messages"][-1]["content"] == "you cited d99"


def test_build_messages_carries_the_system_prompt_and_nothing_else():
    """No few-shot examples: the output format is prose and the grounding comes
    from `documents`, so an example would be a hand-written answer about these
    two regulations that the judge would then grade instead of the model's."""
    messages = build_messages("What is a provider?")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == SYSTEM_PROMPT


def test_the_system_prompt_covers_both_sides_of_the_refusal_tension():
    """`oos-002` says any citation is wrong; `hn-001`/`hn-002` say an uncited
    correct answer is only partial. One prompt has to produce both."""
    lowered = SYSTEM_PROMPT.lower()
    assert "do not contain the answer" in lowered
    assert "outside" in lowered and "scope" in lowered
    assert "premise" in lowered


class FlakyChatClient(FakeChatClient):
    """Fails with a retryable error `failures` times, then succeeds."""

    def __init__(self, failures: int = 2):
        super().__init__([text_block("A.")], [])
        self.failures = failures

    def chat(self, **kwargs):
        import cohere.errors

        if self.failures:
            self.failures -= 1
            raise cohere.errors.TooManyRequestsError(body="429")
        return super().chat(**kwargs)


def test_the_retry_fires_on_a_retryable_error_and_attempts_reflects_it(monkeypatch):
    """Two prior steps shipped a new call site with no retry and both were
    bitten -- Step 4's reranker died on a 429 mid-sweep, Step 5's selector scored
    `th-004` a zero for the same reason. This is the third call site.

    `wait` is neutralised so the test does not sleep through the real backoff.
    """
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _seconds: None)
    result = generate_detailed("q", DOCUMENTS, client=FlakyChatClient(failures=2))
    assert result.answer == "A."
    assert result.attempts == 3


def test_attempts_is_read_from_the_accessor_that_is_not_permanently_empty(monkeypatch):
    """THE INSTRUMENT DEFECT, PINNED.

    `attempts` exists to tell a latency measurement apart from a rate-limit
    measurement -- `reranker.py:393` excludes retried calls from its p50, and
    `query-path.md` concluded that three 80-second stalls "were not retry
    backoff" *because every call reported `attempts=1`*.

    They all reported 1 because they could not report anything else. Since
    tenacity 8.2.3 the `@retry` wrapper runs `copy = self.copy()` per invocation
    and assigns the copy's statistics to `wrapped_f.statistics`, while
    `wrapped_f.retry` stays the original controller that never executes -- so
    `_call.retry.statistics` is permanently `{}` and `.get("attempt_number", 1)`
    always returned the default. All three call sites in this repo read it.

    This test fails if the accessor regresses, which the previous version of
    every one of those sites would.
    """
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _seconds: None)
    for failures, expected in ((0, 1), (1, 2), (3, 4)):
        result = generate_detailed(
            "q", DOCUMENTS, client=FlakyChatClient(failures=failures)
        )
        assert result.attempts == expected, f"{failures} failures should be {expected} attempts"


def test_a_call_that_never_succeeds_surfaces_as_generate_error_not_a_bad_answer(monkeypatch):
    """`reraise=True`, `stop_after_attempt(6)`, and then the reraised
    `TooManyRequestsError` -- an `ApiError` subclass -- becomes this module's own
    type. A rate-limited row has to be visible as an error in the artifact rather
    than scored as a zero, which is what happened to `th-004` in Step 5."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _seconds: None)
    with pytest.raises(GenerateError, match="TooManyRequestsError"):
        generate_detailed("q", DOCUMENTS, client=FlakyChatClient(failures=99))


def test_an_api_error_raises_generate_error_rather_than_exiting_the_process():
    from cohere.core import ApiError

    class Failing:
        def chat(self, **kwargs):
            raise ApiError(status_code=400, body="bad request")

    with pytest.raises(GenerateError, match="generation failed"):
        generate_detailed("q", DOCUMENTS, client=Failing())


def test_a_missing_api_key_raises_generate_error_rather_than_exiting(monkeypatch):
    monkeypatch.setattr("src.config.settings.cohere_api_key", "")
    with pytest.raises(GenerateError, match="is not set"):
        generate_detailed("q", DOCUMENTS)


# --------------------------------------------------------------------------
# `request_sha` -- the E1 instrument
#
# The point of the field is to make "was it the same request?" answerable
# instead of assumed. That only works if the hash covers the body that was
# actually sent, so the tests below pin it to `client.chat`'s own kwargs rather
# than to a reconstruction -- if the two ever drift, the instrument is lying and
# the determinism finding built on it is void.
# --------------------------------------------------------------------------

def _sha_of(client_calls: list[dict]) -> str:
    """Recompute the digest from what the fake client was actually handed."""
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            client_calls[0], sort_keys=True, ensure_ascii=False, default=str
        ).encode("utf-8")
    ).hexdigest()


def test_request_sha_is_the_hash_of_the_body_the_client_was_handed():
    """Not of a reconstruction. `_chat_call` builds one dict, hashes it, and
    splats it -- so the digest and the wire payload cannot disagree."""
    client = FakeChatClient([text_block("a")])
    result = generate_detailed("q", DOCUMENTS, client=client)
    assert result.request_sha == _sha_of(client.calls)


def test_the_same_question_and_documents_hash_the_same():
    first = FakeChatClient([text_block("a")])
    second = FakeChatClient([text_block("DIFFERENT TEXT ENTIRELY")])
    a = generate_detailed("q", DOCUMENTS, client=first)
    b = generate_detailed("q", DOCUMENTS, client=second)
    # Identical body, different response -- which is exactly the case E1 exists
    # to detect, and the hash must not move with the response.
    assert a.request_sha == b.request_sha
    assert a.answer != b.answer


@pytest.mark.parametrize(
    "question, documents",
    [
        ("a different question", DOCUMENTS),
        ("q", [{"id": "d0", "data": {"text": "DIFFERENT", "source": "PASSAGE", "citation": "L"}}]),
        ("q", [*DOCUMENTS, {"id": "d1", "data": {"text": "extra", "source": "GRAPH", "citation": "M"}}]),
    ],
    ids=["question", "document-text", "document-count"],
)
def test_any_change_to_the_payload_changes_the_hash(question, documents):
    """The failure this guards against is a hash that is stable because it is
    blind: one that ignored `documents` would report "same request" across two
    genuinely different context assemblies and misattribute our bug to Cohere."""
    base = generate_detailed("q", DOCUMENTS, client=FakeChatClient([text_block("a")]))
    other = generate_detailed(question, documents, client=FakeChatClient([text_block("a")]))
    assert base.request_sha != other.request_sha


def test_the_regeneration_turn_hashes_differently_from_the_first_call():
    """`answer_path` regenerates with an appended correction turn precisely so
    the second request is NOT the first one repeated. If both hashed the same,
    the determinism test would quietly count a deliberately different request as
    evidence about the provider."""
    first = generate_detailed("q", DOCUMENTS, client=FakeChatClient([text_block("a")]))
    messages = [
        *build_messages("q"),
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "cite only the documents provided"},
    ]
    retry = generate_detailed(
        "q", DOCUMENTS, client=FakeChatClient([text_block("b")]), messages=messages
    )
    assert first.request_sha != retry.request_sha


def test_the_hash_covers_the_decoding_parameters():
    """`temperature`, `seed` and `max_tokens` are in the body, so a change to
    any of them must move the digest -- otherwise raising MAX_TOKENS would look
    like the same request returning different text."""
    client = FakeChatClient([text_block("a")])
    generate_detailed("q", DOCUMENTS, client=client)
    body = client.calls[0]
    assert body["temperature"] == 0
    assert body["seed"] == 42
    assert body["max_tokens"] == MAX_TOKENS
    mutated = {**body, "max_tokens": MAX_TOKENS + 1}
    assert _sha_of([mutated]) != _sha_of([body])
