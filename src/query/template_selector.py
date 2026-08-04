"""Choose which Cypher template answers a question, and with which parameter.

Between the linker (which resolves question spans to live node keys) and
`graph_query.run_template` (which executes one) there was nothing. This module is
that gap: linked entities plus question shape in, a validated plan of
`(template, params)` calls out. Two arms, as ADR-0012 did for routing -- rules and
Command R7B -- measured against each other and adopted on evidence (ADR-0013).

ADR-0002 IS ENFORCED BY `graph_query.validate`, NOT BY THIS MODULE.

Every call either arm produces goes through `validate()` before a driver exists,
so a selector that hallucinates a template name or a parameter raises rather than
reaching Neo4j. That is deliberate: this module is allowed to be wrong, and the
boundary below it is not. The model arm fills parameter *values* freely -- it is
never shown the linker's output -- because "can a small model produce a usable
Cypher parameter" is one of the things being measured, and snapping its answer to
a linked entity would measure the linker instead.

THE METRIC THIS STEP WAS GIVEN DOES NOT WORK, AND THAT WAS MEASURED FIRST.

The phase plan says template-selection accuracy is measurable against each eval
row's `ontology_edges`. It is computable, and it is nearly uninformative, which is
a different thing. Measured 2026-08-04 before either arm existed:

  * All three `ontology_edges` ceilings sit at **10 of 10** on the rows the router
    actually sends to the graph -- a template traverses a declared edge (10/10),
    the linker can fill that template (10/10), and both hold for the same template
    (10/10). A metric whose ceiling is 100% has no headroom to report.
  * The constant arm `always-obligations_for_system` scores **9 of 10** by
    edge-intersection, and `always-obligations_for_role` **8 of 10**. Those two
    templates between them traverse APPLIES_TO, IMPOSES and CLASSIFIED_AS, which
    nearly every row declares.

Ceiling 10, floor 9, so the metric has about one row of discriminating power. This
is exactly the shape ADR-0012 caught R7B with -- a router beaten by the
majority-class constant -- found here before an arm was written rather than after.
`scoreboard` still reports edge-intersection **with its constants printed beside
it**, because the honest way to publish an uninformative number is next to the
thing that makes it uninformative.

THE HEADLINE IS GOLD YIELD, WHICH IS WHAT THE VECTOR PATH IS ALREADY SCORED ON.

Does the executed plan's provenance contain the row's gold `source_chunk_ids`?
That is directly comparable to Step 4's recall, it is not gameable by a constant,
and the oracle for it was measured before either arm existed too: **25 of 35 gold
chunks** over the 10 routed rows, taking the best (template, anchor) pair per row
with the gold visible. `ag-001` reaches **11 of 11** -- the aggregation question
top-10 similarity cannot answer at all, because it has 11 gold chunks.

Usage:
    python -m src.query.template_selector --question "..."
    python -m src.query.template_selector --eval
    python -m src.query.template_selector --eval --refresh   # re-runs R7B (~$0.001)
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config import price_of, settings
from src.query.cypher_templates import TEMPLATES, TEMPLATE_PARAMS
from src.query.entity_linker import LinkedEntity, LinkIndex, build_index, link_detailed
from src.query.graph_query import provenance_of, run_template, validate
from src.schemas import ALLOWED_ENDPOINTS, ENTITY_TYPES, RELATION_TYPES

if TYPE_CHECKING:
    from neo4j import Driver

__all__ = [
    "TEMPLATE_ANCHORS",
    "TEMPLATE_EDGES",
    "SELECTABLE_TYPES",
    "TemplateCall",
    "SelectorResult",
    "SelectorError",
    "select",
    "select_by_rules",
    "select_by_model",
]

ROOT = Path(__file__).resolve().parents[2]
QUESTIONS = ROOT / "eval" / "eval-questions.jsonl"

# Beside the eval set, tracked, not under `data/` -- the reason router.py:66-71
# gives: the tests and the metrics doc read their numbers out of this file and
# both must work with no API key and no spend.
ARTIFACT = ROOT / "eval" / "selector-eval.jsonl"

# Which relationship types each template traverses. This is the only bridge
# between a template name and the eval set's `ontology_edges`, so the whole
# edge-intersection measurement rests on it -- hence the drift test that regexes
# these back out of the Cypher. `path_between` is `[*..4]` and untyped, so it
# traverses anything; an empty set here means "no typed claim", and it is the only
# cover this library has for REFERENCES, LISTED_IN, SETS_PENALTY, EXEMPT_FROM,
# PERMITS and GRANTS -- 6 of the ontology's 13 relation types.
TEMPLATE_EDGES: dict[str, frozenset[str]] = {
    "obligations_for_role": frozenset({"APPLIES_TO", "IMPOSES"}),
    "obligations_for_system": frozenset({"CLASSIFIED_AS", "APPLIES_TO", "IMPOSES"}),
    "enforcement_chain": frozenset({"ENFORCED_BY", "PENALIZED_UNDER"}),
    "definition_of": frozenset({"DEFINED_IN"}),
    "cross_regulation": frozenset({"INTERACTS_WITH"}),
    "path_between": frozenset(),
}

# Which entity types may fill which declared parameter.
#
# Three templates pin a label in the MATCH itself (:ActorRole, :SystemType,
# :Obligation) and for those the set is that one label, checked against the Cypher
# by `test_pinned_anchor_labels_match_the_cypher`. The other three carry only the
# shared :Entity label, so the constraint is not in the query text at all -- it is
# the endpoint contract in src/schemas.py, and taking it from there rather than
# retyping it here is what keeps a widened endpoint from silently leaving this
# table behind. (ALLOWED_ENDPOINTS["INTERACTS_WITH"] is read from BOTH ends
# because `cross_regulation` matches undirected.)
_INTERACTS = frozenset(
    ALLOWED_ENDPOINTS["INTERACTS_WITH"][0] | ALLOWED_ENDPOINTS["INTERACTS_WITH"][1]
)
TEMPLATE_ANCHORS: dict[str, dict[str, frozenset[str]]] = {
    "obligations_for_role": {"role": frozenset({"ActorRole"})},
    "obligations_for_system": {"system_type": frozenset({"SystemType"})},
    "enforcement_chain": {"obligation": frozenset({"Obligation"})},
    "definition_of": {"term": frozenset(ALLOWED_ENDPOINTS["DEFINED_IN"][0])},
    "cross_regulation": {"article": _INTERACTS},
    "path_between": {"entity_a": ENTITY_TYPES, "entity_b": ENTITY_TYPES},
}

# Every type that can fill some declared parameter, ignoring `path_between`
# (which accepts everything and would make the set vacuous).
SELECTABLE_TYPES: frozenset[str] = frozenset().union(
    *(
        frozenset().union(*params.values())
        for name, params in TEMPLATE_ANCHORS.items()
        if name != "path_between"
    )
)

# Six types fill a declared parameter but are absent from `router.ANCHOR_TYPES`,
# whose comment says "none is a parameter any template declares". That is true of
# the three *typed* templates and false of `definition_of`, whose head is
# `(t:Entity)` and whose endpoint contract accepts nine types including
# DefinedTerm, Authority, Right and Penalty.
#
# The router is NOT changed here. ADR-0012's adopted 21 of 22 was measured with
# ANCHOR_TYPES exactly as it stands, and editing it silently re-measures Step 3.
# `test_the_anchor_type_disagreement_is_exactly_as_recorded` pins the difference
# so it cannot drift unnoticed, and docs/metrics/query-path.md carries it as an
# open item for a step that can afford to re-run the router sweep.
ANCHOR_TYPE_DISAGREEMENT: frozenset[str] = frozenset(
    {"Authority", "DefinedTerm", "LawfulBasis", "Penalty", "Regulation", "Right"}
)

# A plan is capped because every call is a real query and two of the templates are
# large: `obligations_for_role('provider')` returns 210 rows and
# `obligations_for_system('high risk ai system')` 169. Three is enough to cover
# the widest oracle row (3h-001 needs definition_of beside the obligation pair)
# and small enough that a plan cannot quietly become "run everything".
MAX_CALLS = 3

# "what does X mean", "definition of X", "how is X defined" -- the shape that says
# the answer is a defining provision rather than a duty.
_DEFINITIONAL = re.compile(
    r"\bdefinition of\b|\bdefined\b|\bwhat (does|is) .{0,60}\bmean\b|\bmeaning of\b", re.I
)

# Two instruments named in one question. `cross_regulation` only pays off when the
# question actually spans the AIA/GDPR boundary; firing it on every Article anchor
# adds a query per question for nothing.
_CROSS_INSTRUMENT = re.compile(r"\bgdpr\b|\bgeneral data protection\b|\bregulation \(eu\) 2016/679\b", re.I)


class SelectorError(RuntimeError):
    """A selector could not produce a plan.

    Deliberately not `SystemExit`, for the reason `RouterError` (router.py:101)
    and `RetrieverError` (retriever.py:58) both give -- this runs inside a FastAPI
    worker at Step 7 and one question getting a 400 must not take the process
    down with it.
    """


@dataclass(frozen=True)
class TemplateCall:
    """One validated template invocation, and why it was chosen."""

    template: str
    params: dict[str, str]
    rule: str
    anchors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # ADR-0002's boundary, applied at construction rather than at execution:
        # an invalid call cannot be built, so it cannot be passed on either.
        validate(self.template, self.params)


@dataclass
class SelectorResult:
    """One selector's answer, with everything the artifact and the ADR need.

    `plan` is empty when the selector found nothing to run -- and empty is also
    what an unparseable model answer produces, with `raw` kept. Nothing is ever
    repaired into a default template: a selector that emits garbage 10% of the
    time is a different fact about the world than one that picks
    `obligations_for_system` 10% of the time, and coercing the first into the
    second deletes the difference. Same discipline as `RouterResult`.
    """

    plan: list[TemplateCall] = field(default_factory=list)
    rule: str | None = None
    raw: str | None = None
    latency_ms: float = 0.0
    cost_usd: float | None = None
    linked: list[LinkedEntity] | None = None
    error: str | None = None
    # 1 means the call succeeded first time and `latency_ms` is the model's own.
    # Anything higher means most of `latency_ms` was retry backoff, and a
    # percentile over retried calls describes the API key rather than the model.
    # Same field, same reason, as `RerankResult.attempts` (reranker.py:105).
    attempts: int = 1


# --------------------------------------------------------------------------
# The deterministic arm
# --------------------------------------------------------------------------

def _first_of_type(entities: list[LinkedEntity], allowed: frozenset[str]) -> LinkedEntity | None:
    """The earliest-mentioned entity whose type can fill a parameter.

    `link_detailed` returns first-mention order and one entry per node, so this
    is deterministic without a sort. First mention rather than longest span
    because a question's subject is usually named before its qualifiers.
    """
    return next((e for e in entities if e.type in allowed), None)


def select_by_rules(
    question: str,
    index: LinkIndex | None = None,
    linked: list[LinkedEntity] | None = None,
) -> SelectorResult:
    """Build a plan from the linked entities and the shape of the question.

    Rules are ordered by how specific the template is, not by how often it fires:
    a definitional question wants `definition_of` even though
    `obligations_for_system` would also have matched its edges. Every rule that
    matches contributes a call, capped at MAX_CALLS, and the rule names travel
    with the calls because "why" is the part a confusion matrix cannot tell you.

    `linked` is an injection point so Step 7 does not link the same question
    twice -- `RouterResult.linked` already holds exactly this list, and
    `router.route()` currently discards it (see query-path.md §Open).
    """
    started = time.perf_counter()
    entities = linked if linked is not None else link_detailed(question, index or build_index())

    calls: list[TemplateCall] = []
    fired: list[str] = []

    def offer(template: str, rule: str, *anchors: LinkedEntity) -> None:
        if len(calls) >= MAX_CALLS or any(c.template == template for c in calls):
            return
        params = dict(zip(sorted(TEMPLATE_PARAMS[template]), (a.canonical_name for a in anchors)))
        calls.append(
            TemplateCall(
                template=template,
                params=params,
                rule=rule,
                anchors=tuple(a.canonical_name for a in anchors),
            )
        )
        fired.append(rule)

    term = _first_of_type(entities, TEMPLATE_ANCHORS["definition_of"]["term"])
    role = _first_of_type(entities, TEMPLATE_ANCHORS["obligations_for_role"]["role"])
    system = _first_of_type(entities, TEMPLATE_ANCHORS["obligations_for_system"]["system_type"])
    obligation = _first_of_type(entities, TEMPLATE_ANCHORS["enforcement_chain"]["obligation"])
    article = _first_of_type(entities, TEMPLATE_ANCHORS["cross_regulation"]["article"])

    # S1 -- a definitional question wants the defining provision, first.
    if term and _DEFINITIONAL.search(question):
        offer("definition_of", "S1-definitional", term)
    # S2 -- an obligation named outright reaches its enforcement chain.
    if obligation:
        offer("enforcement_chain", "S2-obligation", obligation)
    # S3 -- a question naming the other instrument wants the bridge.
    if article and _CROSS_INSTRUMENT.search(question):
        offer("cross_regulation", "S3-cross-instrument", article)
    # S4/S5 -- the two generic duty templates. Ordered role-first: a question that
    # names a party is asking what that party must do, and `obligations_for_role`
    # is the tighter of the two (60 rows for `deployer` against 169).
    if role:
        offer("obligations_for_role", "S4-role", role)
    if system:
        offer("obligations_for_system", "S5-system", system)
    # S6 -- nothing above fired but two anchors exist: ask the graph how they
    # connect. This is the only cover for REFERENCES and LISTED_IN.
    if not calls and len(entities) >= 2:
        offer("path_between", "S6-bridge", entities[0], entities[1])

    return SelectorResult(
        plan=calls,
        rule="+".join(fired) if fired else "S0-none",
        latency_ms=(time.perf_counter() - started) * 1000,
        cost_usd=0.0,
        linked=entities,
    )


# --------------------------------------------------------------------------
# Command R7B
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You pick a knowledge-graph query template for questions about the EU AI Act and the GDPR.

Answer with one line per template, at most 3 lines, in this exact format:

    template_name param=value

Use only these templates and parameters:

obligations_for_role   role=<a party: provider, deployer, importer, distributor, ...>
obligations_for_system system_type=<a kind of AI system>
enforcement_chain      obligation=<a duty>
definition_of          term=<the thing being defined>
cross_regulation       article=<an article or instrument that cites the other regulation>
path_between           entity_a=<one thing> entity_b=<another thing>

Parameter values are matched against node names in the graph, so use the plain
lowercase wording the legislation uses, singular, with no article ("deployer",
not "the deployers"). Output nothing but the lines. If no template fits, answer
exactly: none"""

# Hand-written. NONE of these is in eval/eval-questions.jsonl, and
# tests/test_template_selector.py asserts that against the live eval set rather
# than against this comment -- the same guard ADR-0012 put on the router's
# few-shot after the recurrence tracker's "a prompt few-shot example teaching the
# defect" reached three occurrences.
FEW_SHOT: list[tuple[str, str]] = [
    (
        "What must an importer of a high-risk AI system verify before placing it on the market?",
        "obligations_for_role role=importer",
    ),
    (
        "What does 'serious incident' mean in the AI Act?",
        "definition_of term=serious incident",
    ),
    (
        "Which authority enforces the duty to register in the EU database, and under which article?",
        "enforcement_chain obligation=register in the eu database",
    ),
    (
        "How does AIA Article 10 interact with the GDPR?",
        "cross_regulation article=aia art. 10",
    ),
    (
        "What obligations attach to an emotion recognition system?",
        "obligations_for_system system_type=emotion recognition system",
    ),
    (
        "What is the capital of France?",
        "none",
    ),
]


def build_messages(question: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for example, answer in FEW_SHOT:
        messages.append({"role": "user", "content": example})
        messages.append({"role": "assistant", "content": answer})
    messages.append({"role": "user", "content": question})
    return messages


def parse_plan(text: str) -> tuple[list[TemplateCall], list[str]]:
    """The model's output as validated calls, plus one reason per rejected line.

    Anything that does not name a known template with exactly its declared
    parameters is dropped and the reason recorded -- never repaired, never
    defaulted. `validate()` runs inside `TemplateCall`, so a line naming
    `"'; MATCH (n) DETACH DELETE n //"` is rejected here rather than reaching a
    driver, which is ADR-0002 doing its job at the one boundary the model can
    reach through.
    """
    calls: list[TemplateCall] = []
    rejected: list[str] = []
    for line in text.strip().splitlines():
        line = line.strip().strip("`-* ")
        if not line or line.lower() == "none":
            continue
        head, *rest = line.split()
        params: dict[str, str] = {}
        broken = False
        for token in rest:
            if "=" not in token:
                # A multi-word value: `role=notified body` arrives as two tokens.
                if params:
                    last = list(params)[-1]
                    params[last] = f"{params[last]} {token}".strip()
                else:
                    broken = True
                    break
            else:
                key, _, value = token.partition("=")
                params[key] = value
        if broken:
            rejected.append(f"{line!r}: value before any key=")
            continue
        try:
            calls.append(TemplateCall(template=head, params=params, rule="R7B"))
        except ValueError as exc:
            rejected.append(f"{line!r}: {exc}")
        if len(calls) >= MAX_CALLS:
            break
    return calls, rejected


def get_client() -> Any:
    import cohere

    if not settings.cohere_api_key:
        raise SelectorError(
            f"{settings.cohere_api_key_var} is not set; run --eval without --refresh "
            f"to read the committed artifact instead"
        )
    return cohere.ClientV2(api_key=settings.cohere_api_key)


def _chat_call(client: Any, question: str) -> tuple[Any, int]:
    """The retrying unit, mirroring `reranker._rerank_call`. Returns the attempts.

    The first sweep of this module shipped without it and died the same death the
    reranker did one step earlier: `th-004` came back as a 429 because a Cohere
    trial key allows 20 calls a minute, and the arm under measurement scored a
    zero that was the key's fault rather than the model's. That is the recurrence
    tracker's "verified once by hand, never encoded" wearing a new hat -- the
    lesson was written down in Step 4 and the next new call site still went out
    without it, because nothing in the repo enforced it.
    """
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential_jitter,
    )

    from src.index.embedder import RETRYABLE_ERRORS

    @retry(
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        wait=wait_exponential_jitter(initial=2, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def _call() -> Any:
        return client.chat(
            model=settings.model_router,
            messages=build_messages(question),
            temperature=0,
            seed=42,
            max_tokens=64,
        )

    response = _call()
    return response, _call.retry.statistics.get("attempt_number", 1)


def select_by_model(question: str, client: Any | None = None) -> SelectorResult:
    """Ask Command R7B for a plan. One call, temperature 0, seed fixed."""
    started = time.perf_counter()
    attempts = 1
    try:
        client = client or get_client()
        response, attempts = _chat_call(client, question)
        raw = "".join(item.text for item in response.message.content or []).strip()
        usage = getattr(response, "usage", None)
        billed = getattr(usage, "billed_units", None) if usage else None
        cost = price_of(
            settings.model_router,
            int(getattr(billed, "input_tokens", 0) or 0),
            int(getattr(billed, "output_tokens", 0) or 0),
        )
    except SelectorError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the arm under measurement must not
        # take the sweep down with it; the failure is recorded as this row's result.
        return SelectorResult(
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
            attempts=attempts,
        )

    calls, rejected = parse_plan(raw)
    return SelectorResult(
        plan=calls,
        rule="R7B",
        raw=raw,
        latency_ms=(time.perf_counter() - started) * 1000,
        cost_usd=cost,
        error="; ".join(rejected) or None,
        attempts=attempts,
    )


# --------------------------------------------------------------------------
# The adopted selector
# --------------------------------------------------------------------------

# Set by ADR-0013 from the measurement below, not from preference.
ADOPTED = "rules"


def select(
    question: str,
    index: LinkIndex | None = None,
    linked: list[LinkedEntity] | None = None,
) -> SelectorResult:
    """The adopted selector. `graph_path.graph_search` calls this."""
    if ADOPTED == "rules":
        return select_by_rules(question, index, linked)
    return select_by_model(question)


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

ARMS = ("rules", "r7b")

# Every constant arm, computed rather than swept -- a constant cannot drift, and
# writing it to the artifact would invite someone to edit it. router.py:433-445
# makes the same call for `always-vector`.
CONSTANT_ARMS = tuple(sorted(TEMPLATES))


def load_questions() -> list[dict]:
    return [
        json.loads(line)
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_artifact() -> list[dict]:
    if not ARTIFACT.exists():
        raise SystemExit(
            f"{ARTIFACT} does not exist. Run --eval --refresh with an API key to build it."
        )
    return [
        json.loads(line)
        for line in ARTIFACT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def routed_rows(rows: list[dict]) -> list[dict]:
    """The rows the router sends to the graph. The selector never sees the rest."""
    return [r for r in rows if r["route"] in ("graph", "both")]


# --------------------------------------------------------------------------
# The pre-registration, as code
#
# These three were measured before either arm existed and then, for one commit,
# lived in this module as hand-typed constants -- which is exactly what
# docs/failure-notes.md line 7 says does not count as DONE, and one step below
# the standard `reranker.scoreboard()` already set by computing `cap@k` and
# `oracle@k` from its artifact. They are computed here now, and the published
# values live in tests/test_template_selector.py as assertions rather than as
# sources.
#
# `edge_reachable` and `anchor_fillable` are cheap and pure. The oracle is
# neither: it needs every fillable (template, anchor) pair executed against the
# graph, so like Step 4's stored top-50 it is measured during the sweep and
# carried in the artifact, letting `scoreboard` stay database-free.
# --------------------------------------------------------------------------

def _fillable(entities: list[LinkedEntity], template: str) -> list[LinkedEntity]:
    """The linked entities that can fill `template`'s single declared parameter.

    `path_between` is excluded by callers: it accepts every type on both ends,
    so counting it would make "can the linker fill a template" vacuously true.
    """
    allowed = next(iter(TEMPLATE_ANCHORS[template].values()))
    return [e for e in entities if e.type in allowed]


def _single_param_templates() -> list[str]:
    return [n for n in sorted(TEMPLATE_ANCHORS) if n != "path_between"]


def edge_reachable(row: dict) -> bool:
    """Does any template traverse an edge this row declares?"""
    edges = set(row["ontology_edges"])
    return any(TEMPLATE_EDGES[n] & edges for n in TEMPLATES)


def anchor_fillable(entities: list[LinkedEntity]) -> bool:
    """Can the linker fill any template's parameter for this question?"""
    return any(_fillable(entities, n) for n in _single_param_templates())


def oracle_for_row(
    row: dict, entities: list[LinkedEntity], driver: Driver
) -> tuple[int, dict[str, str] | None]:
    """The best (template, anchor) pair for one row, chosen WITH the gold visible.

    This is the ceiling a perfect selector could reach on this row, and measuring
    it is what lets a weak result be blamed on selection rather than on the graph
    simply not holding the gold. Gated on the declared edges for the same reason
    the pre-registration was: a template that traverses nothing the row declares
    is not a candidate a selector should have found.

    Deliberately the best *single* call, not the best combination. The adopted
    rules may emit up to MAX_CALLS, so they are allowed to beat this; that they
    exactly match it is a finding rather than an arithmetic certainty.
    """
    gold = set(row["source_chunk_ids"])
    edges = set(row["ontology_edges"])
    best_hits, best_choice = 0, None
    for template in _single_param_templates():
        if not (TEMPLATE_EDGES[template] & edges):
            continue
        param = next(iter(TEMPLATE_ANCHORS[template]))
        for entity in _fillable(entities, template):
            found = run_template(template, {param: entity.canonical_name}, driver)
            chunks = {c for r in found for c in provenance_of(r)}
            hits = len(gold & chunks)
            if hits > best_hits:
                best_hits = hits
                best_choice = {"template": template, "anchor": entity.canonical_name}
    return best_hits, best_choice


def sweep(
    rows: list[dict],
    arms: tuple[str, ...] = ARMS,
    driver: Driver | None = None,
    client: Any | None = None,
    conn: Any | None = None,
    plans: dict[tuple[str, str], dict] | None = None,
) -> list[dict]:
    """Run both arms over the eval set, execute what they select, and render it.

    The only impure part. Executes each plan so the artifact carries what the
    graph actually returned -- rows, provenance, the gold intersection, the
    number of statements the rows rendered into, and the oracle -- which is what
    lets `scoreboard` recompute every published number with no database and no
    key.

    Rendering is included rather than left to a second pass because "how many
    questions can the graph path answer at all" is a statement count, not a row
    count: 169 rows carrying one repeated fact is one statement, and reporting
    the row count as reach would restate the hot fact as a result.

    THE ARTIFACT HOLDS TWO KINDS OF THING, AND `plans` IS THE SEAM.

      * What the model said -- `plan`, `rule`, `raw`, `cost_usd`, `latency_ms`,
        `attempts`, `error`. Costs money, and R7B is **not reproducible** at
        `temperature=0, seed=42` (two sweeps gave 16 then 14 gold hits), so it is
        recorded once and never regenerated casually.
      * What the graph did with it -- everything else. Fully deterministic given
        a plan and a loaded graph.

    Passing `plans` (keyed by `(id, arm)`) replays the first group instead of
    re-deriving it and recomputes the second, which is what `--rebuild` does: it
    adds or corrects graph-side fields with no API key, no spend, and without
    moving a single number ADR-0013 quotes.
    """
    from src.answer.path_to_prose import path_to_prose
    from src.query.graph_path import label_map

    owned = driver is None
    if driver is None:
        from src.ingest.graph_writer import connect

        driver = connect()

    index = build_index()
    artifact: list[dict] = []
    try:
        for row in rows:
            entities = link_detailed(row["question"], index)
            oracle_hits, oracle_choice = 0, None
            if row["route"] in ("graph", "both"):
                oracle_hits, oracle_choice = oracle_for_row(row, entities, driver)

            for arm in arms:
                recorded = (plans or {}).get((row["id"], arm))
                if recorded is not None:
                    result = SelectorResult(
                        plan=[
                            TemplateCall(
                                template=c["template"], params=c["params"], rule=c["rule"]
                            )
                            for c in recorded["plan"]
                        ],
                        rule=recorded["rule"],
                        raw=recorded["raw"],
                        latency_ms=recorded["latency_ms"],
                        cost_usd=recorded["cost_usd"],
                        error=recorded.get("error"),
                        attempts=recorded.get("attempts", 1),
                        linked=entities,
                    )
                elif arm == "rules":
                    result = select_by_rules(row["question"], index, entities)
                else:
                    result = select_by_model(row["question"], client)

                gold = set(row["source_chunk_ids"])
                provenance: list[str] = []
                returned = 0
                empty = 0
                exec_error = None
                executed: list[tuple[str, list[dict]]] = []
                for call in result.plan:
                    try:
                        got = run_template(call.template, call.params, driver)
                    except Exception as exc:  # noqa: BLE001 -- recorded per row
                        exec_error = f"{type(exc).__name__}: {exc}"
                        continue
                    returned += len(got)
                    empty += not got
                    if got:
                        executed.append((call.template, got))
                    for r in got:
                        provenance.extend(provenance_of(r))
                provenance = list(dict.fromkeys(provenance))

                labels = label_map(provenance, conn)
                statements: set[str] = set()
                derived_docs = 0
                caveated = 0
                for template, got in executed:
                    try:
                        for doc in path_to_prose(got, template, labels=labels):
                            if doc.text in statements:
                                continue
                            statements.add(doc.text)
                            derived_docs += doc.derived
                            caveated += "unverified annex section" in doc.text
                    except Exception as exc:  # noqa: BLE001 -- recorded per row
                        exec_error = exec_error or f"{type(exc).__name__}: {exc}"

                artifact.append(
                    {
                        "id": row["id"],
                        "stratum": row["stratum"],
                        "route": row["route"],
                        "routed": row["route"] in ("graph", "both"),
                        "selector": arm,
                        "plan": [
                            {"template": c.template, "params": c.params, "rule": c.rule}
                            for c in result.plan
                        ],
                        "rule": result.rule,
                        "raw": result.raw,
                        "ontology_edges": row["ontology_edges"],
                        "gold": sorted(gold),
                        "gold_hits": sorted(gold & set(provenance)),
                        "provenance": provenance,
                        "rows_returned": returned,
                        # A call can pass `validate()` and still return nothing,
                        # because validation checks parameter *names* and the
                        # graph matches on parameter *values*. This counts those
                        # separately -- it is the whole R7B finding (see ADR-0013)
                        # and it is invisible in `rows_returned` alone.
                        "empty_calls": empty,
                        # Statements, not rows. 169 rows carrying one repeated
                        # fact render to one statement; reporting rows as reach
                        # would restate the hot fact as a result.
                        "docs_rendered": len(statements),
                        "docs_derived": derived_docs,
                        "docs_annex_caveated": caveated,
                        # The pre-registration, carried per row so `scoreboard`
                        # can sum it without a database. `oracle_hits` is the
                        # best single (template, anchor) pair for this row,
                        # chosen with the gold visible.
                        "oracle_hits": oracle_hits,
                        "oracle_choice": oracle_choice,
                        "edge_reachable": edge_reachable(row),
                        "anchor_fillable": anchor_fillable(entities),
                        "latency_ms": round(result.latency_ms, 2),
                        "attempts": result.attempts,
                        "cost_usd": result.cost_usd,
                        # Split so `--rebuild` can replay the selector half and
                        # recompute the graph half. Merging them meant a stale
                        # execution failure would be preserved as though it were
                        # something the model did.
                        "error": result.error,
                        "exec_error": exec_error,
                    }
                )
    finally:
        if owned:
            driver.close()
    return artifact


def _edge_hit(templates: list[str], edges: set[str]) -> bool:
    """Does any selected template traverse any declared edge?

    `path_between` is untyped and would trivially satisfy this for every row, so
    it is excluded from the edge metric rather than allowed to inflate it.
    """
    return any(TEMPLATE_EDGES[t] & edges for t in templates)


def scoreboard(rows: list[dict], artifact: list[dict]) -> dict[str, Any]:
    """Every published number, recomputed from the artifact alone.

    Pure -- no database, no API key, no network. That is what lets the tests and
    docs/metrics/query-path.md assert on these figures.
    """
    from eval.run_benchmark import bucket_of

    scored = {r["id"] for r in routed_rows(rows) if bucket_of(r) != "expected_fail"}
    by_row = {r["id"]: r for r in rows}
    board: dict[str, Any] = {"_scored_ids": sorted(scored)}

    for arm in ARMS:
        entries = [e for e in artifact if e["selector"] == arm and e["id"] in scored]
        gold_total = sum(len(e["gold"]) for e in entries)
        gold_hit = sum(len(e["gold_hits"]) for e in entries)
        per_stratum: dict[str, dict[str, int]] = {}
        for e in entries:
            cell = per_stratum.setdefault(e["stratum"], {"hit": 0, "gold": 0, "rows": 0})
            cell["hit"] += len(e["gold_hits"])
            cell["gold"] += len(e["gold"])
            cell["rows"] += 1
        board[arm] = {
            "n": len(entries),
            "gold_hit": gold_hit,
            "gold_total": gold_total,
            "rows_with_a_hit": sum(1 for e in entries if e["gold_hits"]),
            "rows_with_a_plan": sum(1 for e in entries if e["plan"]),
            "rows_returning_rows": sum(1 for e in entries if e["rows_returned"]),
            "edge_hit": sum(
                1
                for e in entries
                if _edge_hit([c["template"] for c in e["plan"]], set(e["ontology_edges"]))
            ),
            "calls": sum(len(e["plan"]) for e in entries),
            "empty_calls": sum(e.get("empty_calls", 0) for e in entries),
            "rows_returned": sum(e["rows_returned"] for e in entries),
            "docs_rendered": sum(e.get("docs_rendered", 0) for e in entries),
            "docs_derived": sum(e.get("docs_derived", 0) for e in entries),
            "docs_annex_caveated": sum(e.get("docs_annex_caveated", 0) for e in entries),
            "rows_answerable": sum(1 for e in entries if e.get("docs_rendered", 0)),
            "docs_upper_bound": sum(len(e["provenance"]) for e in entries),
            "errors": sum(1 for e in entries if e["error"]),
            "exec_errors": sum(1 for e in entries if e.get("exec_error")),
            "retried": [e["id"] for e in entries if e.get("attempts", 1) > 1],
            "cost_usd": sum(e["cost_usd"] or 0.0 for e in entries),
            "latency_ms": sorted(
                e["latency_ms"] for e in entries if e.get("attempts", 1) == 1
            ),
            "per_stratum": per_stratum,
        }

    # The constants, computed not stored.
    board["_constants"] = {
        name: sum(
            1 for rid in sorted(scored) if _edge_hit([name], set(by_row[rid]["ontology_edges"]))
        )
        for name in CONSTANT_ARMS
    }

    # The pre-registration, recomputed rather than recalled. One entry per row
    # per arm carries these identically, so read them off whichever arm is
    # present rather than double-counting.
    #
    # ONE DENOMINATOR, AND MIXING TWO IS A DEFECT THIS FILE ALREADY COMMITTED
    # ONCE. The router sends 10 rows to the graph, but `3h-002` carries
    # `expected_fail` (ADR-0007) and `bucket_of` drops it, so 9 are scored. An
    # earlier `_report` printed "Ceiling 10 of 9" -- the routed-set ceiling over
    # the scored-set denominator, a ratio above 1 that reads as slack which does
    # not exist. Everything here is on the scored set; the routed-set figures
    # live in the module docstring where nothing can divide them.
    first = {e["id"]: e for e in artifact if e["selector"] == ARMS[0] and e["id"] in scored}
    board["oracle"] = sum(e.get("oracle_hits", 0) for e in first.values())
    board["oracle_choices"] = {
        rid: e.get("oracle_choice") for rid, e in sorted(first.items())
    }
    board["ceiling_edge"] = sum(1 for e in first.values() if e.get("edge_reachable"))
    board["ceiling_anchor"] = sum(1 for e in first.values() if e.get("anchor_fillable"))
    board["gold_total"] = sum(len(e["gold"]) for e in first.values())
    board["_n_scored"] = len(scored)
    return board


def _report(rows: list[dict], artifact: list[dict]) -> int:
    board = scoreboard(rows, artifact)
    n = board["_n_scored"]

    print(f"Template selection over the {n} eval rows the router sends to the graph")
    print(f"(of {len(rows)} rows; the other {len(rows) - n} route to `vector` and never reach a selector)\n")

    print(f"{'arm':8} {'gold hits':>10} {'rows w/hit':>12} {'calls':>6} {'0-row calls':>12} {'edge-hit':>10} {'errors':>7}")
    print("-" * 76)
    for arm in ARMS:
        a = board[arm]
        print(
            f"{arm:8} {a['gold_hit']:>4} of {a['gold_total']:<3} {a['rows_with_a_hit']:>8} of {n:<2} "
            f"{a['calls']:>6} {a['empty_calls']:>8} of {a['calls']:<2} {a['edge_hit']:>7} of {n:<2} {a['errors']:>7}"
        )
    print(
        f"{'ORACLE':8} {board['oracle']:>4} of {board['gold_total']:<3}   "
        f"(best single call per row, chosen with the gold visible)"
    )

    print("\nEDGE-INTERSECTION IS REPORTED WITH ITS CONSTANTS, BECAUSE IT IS NEARLY USELESS")
    print("-" * 76)
    for name, hits in sorted(board["_constants"].items(), key=lambda kv: -kv[1]):
        print(f"  always-{name:24} {hits:>2} of {n}")
    print(
        f"\n  Ceiling {board['ceiling_edge']} of {n} (a template traverses a declared edge); "
        f"the linker can\n  fill one on {board['ceiling_anchor']} of {n}. Best constant "
        f"{max(board['_constants'].values())} of {n} -- about one row of\n"
        f"  discriminating power, measured before either arm was written. Gold yield is\n"
        f"  the headline, and all three of these are recomputed here, not recalled."
    )

    print("\nCALLS THAT VALIDATED AND RETURNED NOTHING")
    print("-" * 76)
    for arm in ARMS:
        a = board[arm]
        print(f"  {arm:8} {a['empty_calls']:>2} of {a['calls']:<2} calls matched no node")
    print(
        "\n  `validate()` checks parameter NAMES; the graph matches parameter VALUES.\n"
        "  A call can clear ADR-0002's boundary and still return zero rows -- measured\n"
        "  live: system_type='high-risk AI system' -> 0 rows, 'high risk ai system' ->\n"
        "  169; article='gdpr' -> 0 rows, 'GDPR' -> 29. See ADR-0013."
    )

    print("\nWHAT THE GRAPH PATH ACTUALLY PRODUCES")
    print("-" * 76)
    print(f"  {'arm':8} {'answerable':>11} {'rows':>7} {'statements':>11} {'derived':>8} {'annex-caveat':>13}")
    for arm in ARMS:
        a = board[arm]
        print(
            f"  {arm:8} {a['rows_answerable']:>6} of {n:<2} {a['rows_returned']:>7} "
            f"{a['docs_rendered']:>11} {a['docs_derived']:>8} {a['docs_annex_caveated']:>13}"
        )
    print(
        "\n  `statements` is what reaches a prompt, and it is not `rows`: the 169 rows of\n"
        "  obligations_for_system('high risk ai system') carry ONE classification fact\n"
        "  asserted by 124 chunks, and it renders once. Reporting rows as reach would be\n"
        "  the hot fact restated as a result (graph-load.md:225)."
    )

    print("\nPER STRATUM (gold hits / gold available)")
    print("-" * 72)
    strata = sorted({s for arm in ARMS for s in board[arm]["per_stratum"]})
    print(f"  {'stratum':17} " + "  ".join(f"{a:>12}" for a in ARMS))
    for stratum in strata:
        cells = []
        for arm in ARMS:
            c = board[arm]["per_stratum"].get(stratum)
            cells.append(f"{c['hit']:>5} / {c['gold']:<4}" if c else f"{'-':>12}")
        print(f"  {stratum:17} " + "  ".join(cells))

    print("\nCOST AND LATENCY  (over calls that did not retry -- reranker.py:393)")
    print("-" * 76)
    for arm in ARMS:
        a = board[arm]
        lat = a["latency_ms"]
        p50 = lat[len(lat) // 2] if lat else 0.0
        retried = f"   retried: {', '.join(a['retried'])}" if a["retried"] else ""
        print(
            f"  {arm:8} ${a['cost_usd']:.6f}   p50 {p50:.1f} ms   "
            f"max {max(lat, default=0.0):.1f} ms   n={len(lat)}{retried}"
        )

    winner = max(ARMS, key=lambda a: board[a]["gold_hit"])
    print(f"\nADOPTED = {ADOPTED!r}; best gold yield this run = {winner!r}")
    return 0 if board[ADOPTED]["gold_hit"] >= board["_constants"].get("obligations_for_system", 0) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--question", help="select with both arms for one question")
    parser.add_argument("--eval", action="store_true", help="the full table over the eval set")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=f"re-run both arms live and rewrite {ARTIFACT.name} (needs an API key, ~$0.001)",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="re-execute the committed plans against the graph and rewrite the "
        "graph-side fields; no API key, no spend, model answers untouched",
    )
    parser.add_argument("--json", action="store_true", help="raw result as JSON")
    args = parser.parse_args()

    if not args.question and not args.eval and not args.rebuild:
        parser.error("pass --question, --eval or --rebuild")
    if args.refresh and args.rebuild:
        parser.error("--refresh re-asks the model; --rebuild replays it. Pick one.")

    if args.question:
        rules = select_by_rules(args.question)
        out: dict[str, Any] = {
            "rules": {
                "plan": [{"template": c.template, "params": c.params, "rule": c.rule} for c in rules.plan],
                "rule": rules.rule,
                "latency_ms": round(rules.latency_ms, 2),
                "linked": [f"{e.canonical_name} ({e.type})" for e in rules.linked or []],
            }
        }
        if settings.cohere_api_key:
            model = select_by_model(args.question)
            out["r7b"] = {
                "plan": [{"template": c.template, "params": c.params} for c in model.plan],
                "raw": model.raw,
                "latency_ms": round(model.latency_ms, 2),
                "cost_usd": model.cost_usd,
                "error": model.error,
            }
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            for arm, body in out.items():
                print(f"{arm}:")
                for call in body["plan"]:
                    print(f"    {call['template']}  {call['params']}")
                if not body["plan"]:
                    print("    (no plan)")
                if body.get("raw") is not None:
                    print(f"    raw: {body['raw']!r}")
                if body.get("error"):
                    print(f"    error: {body['error']}")
        return 0

    rows = load_questions()
    if args.refresh:
        artifact = sweep(rows)
    elif args.rebuild:
        recorded = {(e["id"], e["selector"]): e for e in load_artifact()}
        artifact = sweep(rows, plans=recorded)
    else:
        artifact = load_artifact()

    if args.refresh or args.rebuild:
        ARTIFACT.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in artifact) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {len(artifact)} rows to {ARTIFACT}\n")
    return _report(rows, artifact)


if __name__ == "__main__":
    raise SystemExit(main())
