"""Which retrieval path a question takes: `graph`, `vector`, or `both`.

TWO ROUTERS, MEASURED AGAINST EACH OTHER AND AGAINST A CONSTANT.

The roadmap specifies Command R7B here and treats using the small model for a
three-way classification as a cost-engineering signal. This project has twice
found the deterministic stage was the one that worked -- ADR-0009 is an entire
ADR about an embedding stage that scored *below* its own control -- so both are
built, both are measured on the same 23 hand-labelled rows, and ADR-0012 records
the adoption together with the loser's numbers.

Two constant arms are reported alongside them. `always-vector` is the
majority-class baseline (13 of 23 gold labels are `vector`); `always-both` is a
defensible production router that simply runs both paths every time. A router
that cannot beat a constant has not earned its place in the request path, and
saying so requires the constants to be in the table rather than assumed.

THE COMPARISON IS ASYMMETRIC AND THAT IS RECORDED, NOT SMOOTHED OVER.

The rules below were authored with all 23 gold labels visible, so their accuracy
is an in-sample number and an upper bound. R7B saw none of them: its few-shot
examples are hand-written questions about the same two regulations that appear
nowhere in `eval/eval-questions.jsonl`, which `tests/test_router.py` asserts
mechanically rather than trusting this comment. Reusing eval questions as
few-shot examples would be leakage and would make the whole measurement
meaningless -- if you add examples later, keep them out of the eval set.

THE ADOPTION CRITERION WAS FIXED BEFORE THE MEASUREMENT RAN.

  1. accuracy over the gold rows
  2. a gap of <= 1 row is a tie, and the tie goes to the deterministic baseline,
     which costs $0.00 and adds no network hop
  3. either router fails outright if it sends a `graph_traversable: false` row to
     `graph` -- xr-003 and xr-004 have no derivable article-level bridge, a fact
     established twice independently (the eval author read the law; Step 2's
     linker reached nothing their gold chunks assert)

Usage:
    python -m src.query.router --question "..."       # both routers, one question
    python -m src.query.router --eval                 # the table, from the artifact
    python -m src.query.router --eval --refresh       # re-run R7B live (~$0.0005)
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import price_of, settings
from src.query import decision_log
from src.query.entity_linker import LinkIndex, LinkedEntity, build_index, link_detailed
from src.schemas import ROUTES, Route

__all__ = ["Route", "ROUTES", "RouterResult", "RouterError", "route", "route_by_rules", "route_by_model"]

ROOT = Path(__file__).resolve().parents[2]
QUESTIONS = ROOT / "eval" / "eval-questions.jsonl"

# Beside the eval set it measures, and tracked, because the tests and the metrics
# doc read their numbers out of it -- both must work with no API key and no spend.
# NOT under `data/`, which .gitignore excludes wholesale: the decision log lives
# there because it is generated and grows without bound, but a measurement whose
# numbers are quoted in an ADR has to be in the repo next to the ADR.
ARTIFACT = ROOT / "eval" / "router-eval.jsonl"

# The entity types the six templates will actually accept as an anchor.
# `obligations_for_role` takes an :ActorRole, `obligations_for_system` a
# :SystemType, `enforcement_chain` an :Obligation, `cross_regulation` an entity
# named like an :Article. :Annex and :RiskCategory reach the classification chain.
# Deliberately absent: :Regulation, :DefinedTerm, :Authority, :Penalty. Every one
# of those is a real node a question names in passing -- `GDPR`, `infringement`,
# `administrative fine` -- and none is a parameter any template declares, so a
# question reaching only those has nothing to traverse from.
ANCHOR_TYPES = frozenset(
    {"ActorRole", "SystemType", "Obligation", "Article", "RiskCategory", "Annex"}
)

# "List X", "Which Y are/fall/apply ..." -- the answer is a set, not a sentence.
_ENUMERATIVE = re.compile(r"\blist\b|\bwhich .{0,40}\b(are|fall|apply)\b|\bwhat are\b", re.I)

# A conjoined second question ("..., and who does that obligation fall on?").
# The second ask is a second hop: something related to the first answer, which is
# exactly the shape an edge exists to express.
_SECOND_ASK = re.compile(r",? and (who|what|how|where|does|is|are|against)\b", re.I)

# ...except when the second ask is about the *same* subject, carried by a pronoun:
# "and is it a one-time or ongoing process?", "and does it require a notified
# body?". That is a further property of one thing, answered by the same passage,
# not a hop to another thing. Without this distinction sh-001 and sh-002 both read
# as two-hop questions and route to `both`.
_PRONOUN_TAIL = re.compile(r",? and (is|does|was|can|must|will) it\b", re.I)


class RouterError(RuntimeError):
    """A router could not produce a route.

    Deliberately not `SystemExit`. `src/index/embedder.py:141` converts a
    non-retryable `ApiError` into `SystemExit`, which is right for a CLI and
    wrong here: this code runs inside a FastAPI worker at Step 7, and one
    question getting a 400 must not take the process down with it.
    """


@dataclass
class RouterResult:
    """One router's answer, with everything the decision log and the ADR need.

    `route` is None when the model returned something that is not a route. That
    is recorded and counted as a misroute rather than repaired into `both`: a
    router that emits garbage 10% of the time is a different fact about the world
    than a router that emits `both` 10% of the time, and coercing the first into
    the second deletes the difference.
    """

    route: Route | None
    rule: str | None = None
    raw: str | None = None
    latency_ms: float = 0.0
    cost_usd: float | None = None
    linked: list[LinkedEntity] | None = None
    error: str | None = None


# --------------------------------------------------------------------------
# The deterministic baseline
# --------------------------------------------------------------------------

def route_by_rules(question: str, index: LinkIndex | None = None) -> RouterResult:
    """Route from the linked entities and the shape of the question. No API call.

    Rules are ordered and the first match wins; the name of the rule that fired
    is returned alongside the route, because "why" is the part a confusion matrix
    cannot tell you and the part Phase 5 will want.

    R1 is inert on the current eval set and is kept anyway. Step 2 measured the
    link rate at 23 of 23, so "a question that links to zero nodes has no graph
    path available" -- which the phase plan called a strong rule -- fires on no
    row here. It is a real guard on the request path, where a question about
    something the corpus has never heard of will link to nothing, and it is
    reported as inert rather than quietly counted as a rule that works.
    """
    started = time.perf_counter()
    entities = link_detailed(question, index or build_index())
    types = {e.type for e in entities}

    if not entities:
        route_, rule = "vector", "R1-no-links"
    elif not (types & ANCHOR_TYPES):
        route_, rule = "vector", "R2-no-anchor"
    elif (
        _ENUMERATIVE.search(question)
        and not _SECOND_ASK.search(question)
        and "ActorRole" in types
    ):
        route_, rule = "graph", "R3-enumerate-role"
    elif _SECOND_ASK.search(question) and not _PRONOUN_TAIL.search(question):
        route_, rule = "both", "R4-second-ask"
    else:
        route_, rule = "vector", "R5-default"

    return RouterResult(
        route=route_,
        rule=rule,
        latency_ms=(time.perf_counter() - started) * 1000,
        cost_usd=0.0,
        linked=entities,
    )


# --------------------------------------------------------------------------
# Command R7B
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You route questions about the EU AI Act and the GDPR to a retrieval path.

Answer with exactly one word: graph, vector, or both.

graph  - the answer is a set or a chain a knowledge graph can traverse: every
         obligation attached to a role, a classification chain, an enforcement
         chain. Similarity search would have to guess how many passages to return.
vector - the answer sits in one or two passages that state it directly, or the
         question is out of scope, or it asks for something the corpus does not
         contain. Nothing needs to be traversed.
both   - the question asks two connected things: a fact and then something
         related to it, or one regulation and then the other. The link is an
         edge; the substance is passage text."""

# Hand-written. NONE of these is in eval/eval-questions.jsonl, and
# tests/test_router.py asserts that against the live eval set rather than against
# this comment. See the module docstring on leakage.
FEW_SHOT: list[tuple[str, str]] = [
    ("What does 'serious incident' mean in the AI Act?", "vector"),
    ("List every obligation the AI Act places on importers of high-risk AI systems.", "graph"),
    (
        "Which authority supervises providers of general-purpose AI models, and what "
        "penalty can it impose?",
        "both",
    ),
    (
        "What is the deadline for notifying a personal data breach to the supervisory "
        "authority under the GDPR?",
        "vector",
    ),
    ("List the tasks of the European Data Protection Board.", "graph"),
    (
        "An AI system is a safety component of a machine covered by Union harmonisation "
        "legislation. How does the AI Act classify it, and who carries out the conformity "
        "assessment?",
        "both",
    ),
]


def build_messages(question: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for example, answer in FEW_SHOT:
        messages.append({"role": "user", "content": example})
        messages.append({"role": "assistant", "content": answer})
    messages.append({"role": "user", "content": question})
    return messages


def parse_route(text: str) -> Route | None:
    """The model's output as a route, or None.

    Whitespace and a trailing period are tolerated because they are formatting,
    not content. Anything else -- a sentence, an explanation, a fourth category --
    is None. Searching the text for a route word would turn "this is not a graph
    question" into `graph`, which is the failure mode this function exists to
    prevent.
    """
    token = text.strip().strip(".\"'` \n").lower()
    return token if token in ROUTES else None  # type: ignore[return-value]


def get_client() -> Any:
    import cohere

    if not settings.cohere_api_key:
        raise RouterError(
            f"{settings.cohere_api_key_var} is not set; run --eval without --refresh "
            f"to read the committed artifact instead"
        )
    return cohere.ClientV2(api_key=settings.cohere_api_key)


def route_by_model(question: str, client: Any | None = None) -> RouterResult:
    """One Command R7B call, one word out.

    Mirrors `src/ingest/extract.py:call_model` -- temperature 0, fixed seed, the
    same retryable-error set -- because that is the only chat call site in this
    repo that has survived 1,108 requests, and a second dialect of the same call
    would be a second thing to get wrong.
    """
    from cohere.core import ApiError
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential_jitter,
    )

    from src.ingest.extract import RETRYABLE_ERRORS

    client = client or get_client()

    @retry(
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        wait=wait_exponential_jitter(initial=2, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _call() -> Any:
        return client.chat(
            model=settings.model_router,
            messages=build_messages(question),
            temperature=0,
            seed=42,
            # One word out. Small enough that a model trying to explain itself
            # truncates and is caught below, rather than producing a long answer
            # whose first word happens to look like a route.
            max_tokens=8,
        )

    started = time.perf_counter()
    try:
        res = _call()
    except ApiError as exc:
        # Not SystemExit. See RouterError.
        return RouterResult(
            route=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    raw = "".join(block.text for block in (res.message.content or []))
    tokens = res.usage.tokens if res.usage else None
    tokens_in = int(tokens.input_tokens or 0) if tokens else 0
    tokens_out = int(tokens.output_tokens or 0) if tokens else 0

    # extract.py:499 checks this the same way. A truncated answer that happens to
    # start with a route word must not be read as a confident classification.
    truncated = str(res.finish_reason).upper().endswith("MAX_TOKENS")
    parsed = None if truncated else parse_route(raw)

    return RouterResult(
        route=parsed,
        raw=raw,
        latency_ms=latency_ms,
        cost_usd=price_of(settings.model_router, tokens_in, tokens_out),
        error="finish_reason=MAX_TOKENS" if truncated else None,
    )


# --------------------------------------------------------------------------
# The adopted router
# --------------------------------------------------------------------------

# Set by ADR-0012 from the measurement in docs/metrics/query-path.md, not by
# preference. Change it there and here together, or the ADR stops describing the
# code it claims to describe.
ADOPTED = "rules"


def route(question: str, *, log: bool = True, run_id: str | None = None) -> Route:
    """Classify a question into graph / vector / both.

    Logs the decision by default; `/ask` (Step 7) passes the request's own run_id
    so one session's decisions group together.
    """
    result = route_by_rules(question) if ADOPTED == "rules" else route_by_model(question)
    if result.route is None:
        raise RouterError(
            f"{ADOPTED} router returned no route: raw={result.raw!r} error={result.error}"
        )
    if log:
        decision_log.append(
            decision_log.Decision(
                run_id=run_id or decision_log.new_run_id(),
                question=question,
                router=ADOPTED,
                route=result.route,
                raw=result.raw,
                rule=result.rule,
                latency_ms=result.latency_ms,
                cost_usd=result.cost_usd,
                linked=[e.canonical_name for e in result.linked or []],
            )
        )
    return result.route


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

ARMS = ("rules", "r7b", "always-vector", "always-both")
CONSTANT_ARMS = {"always-vector": "vector", "always-both": "both"}


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


def sweep(rows: list[dict], run_id: str, log_path: Path | None = None) -> list[dict]:
    """Run both real routers over every eval row. Costs about $0.0005."""
    index = build_index()
    client = get_client()
    out: list[dict] = []

    for row in rows:
        for arm, result in (
            ("rules", route_by_rules(row["question"], index)),
            ("r7b", route_by_model(row["question"], client)),
        ):
            out.append({
                "run_id": run_id,
                "id": row["id"],
                "stratum": row["stratum"],
                "router": arm,
                "route": result.route,
                "gold": row["route"],
                "raw": result.raw,
                "rule": result.rule,
                "latency_ms": round(result.latency_ms, 2),
                "cost_usd": result.cost_usd,
                "error": result.error,
            })
            decision_log.append(
                decision_log.Decision(
                    run_id=run_id,
                    question=row["question"],
                    question_id=row["id"],
                    router=arm,
                    route=result.route,
                    raw=result.raw,
                    rule=result.rule,
                    latency_ms=result.latency_ms,
                    cost_usd=result.cost_usd,
                    linked=[e.canonical_name for e in result.linked or []],
                    gold=row["route"],
                    error=result.error,
                ),
                log_path,
            )
    return out


def predictions(rows: list[dict], artifact: list[dict]) -> dict[str, dict[str, str | None]]:
    """{arm: {question_id: route}} for all four arms.

    The two constant arms are computed rather than stored: a constant cannot
    drift, and writing it to the artifact would invite someone to edit it.
    """
    by_arm: dict[str, dict[str, str | None]] = {arm: {} for arm in ARMS}
    for entry in artifact:
        if entry["router"] in by_arm:
            by_arm[entry["router"]][entry["id"]] = entry["route"]
    for arm, constant in CONSTANT_ARMS.items():
        by_arm[arm] = {row["id"]: constant for row in rows}
    return by_arm


def scoreboard(rows: list[dict], artifact: list[dict]) -> dict[str, dict[str, Any]]:
    """Accuracy and the hard gate per arm. Pure, so a test can assert on it."""
    from eval.run_benchmark import bucket_of

    gold = {row["id"]: row["route"] for row in rows}
    by_arm = predictions(rows, artifact)
    excluded = {row["id"] for row in rows if bucket_of(row)}
    scored = [qid for qid in gold if qid not in excluded]
    no_bridge = {row["id"] for row in rows if row.get("graph_traversable") is False}

    out: dict[str, dict[str, Any]] = {"_scored_ids": scored}
    for arm in ARMS:
        preds = by_arm[arm]
        correct = sum(preds.get(qid) == gold[qid] for qid in scored)
        out[arm] = {
            "correct": correct,
            "scored": len(scored),
            "accuracy": correct / len(scored),
            "gate_ok": all(preds.get(qid) != "graph" for qid in no_bridge),
            "misses": [(qid, gold[qid], preds.get(qid)) for qid in scored
                       if preds.get(qid) != gold[qid]],
        }
    return out


def _report(rows: list[dict], artifact: list[dict]) -> int:
    board = scoreboard(rows, artifact)
    scored = board["rules"]["scored"]
    no_bridge = sorted(row["id"] for row in rows if row.get("graph_traversable") is False)

    print(f"\nRouter accuracy over {scored} rows "
          f"({len(rows) - scored} in the expected_fail bucket, reported separately)\n")
    print(f"{'arm':16} {'correct':>9} {'accuracy':>9} {'gate':>6}   cost/query    latency p50")
    print("-" * 76)

    for arm in ARMS:
        stats = board[arm]
        costs = [e["cost_usd"] for e in artifact
                 if e["router"] == arm and e["cost_usd"] is not None]
        lats = sorted(e["latency_ms"] for e in artifact if e["router"] == arm)
        cost = f"${sum(costs) / len(costs):.8f}" if costs else "$0.00000000"
        p50 = f"{lats[len(lats) // 2]:.1f} ms" if lats else "0.0 ms"
        print(f"{arm:16} {stats['correct']:>4}/{scored:<4} {stats['accuracy']:>8.0%} "
              f"{'ok' if stats['gate_ok'] else 'FAIL':>6}   {cost:>11}   {p50:>12}")

    print(f"\ngate = never routes a graph_traversable:false row "
          f"({', '.join(no_bridge)}) to `graph`")

    gold = {row["id"]: row["route"] for row in rows}
    all_preds = predictions(rows, artifact)
    for arm in ("rules", "r7b"):
        preds = all_preds[arm]
        print(f"\n{arm}: confusion (rows = gold, cols = predicted)")
        header = [*sorted(ROUTES), "none"]
        print(f"{'':10}" + "".join(f"{c:>8}" for c in header))
        for g in sorted(ROUTES):
            counts = collections.Counter(
                preds.get(qid) or "none"
                for qid in board["_scored_ids"] if gold[qid] == g
            )
            print(f"{g:10}" + "".join(f"{counts.get(c, 0):>8}" for c in header))
        for qid, want, got in board[arm]["misses"]:
            print(f"    miss {qid}: gold={want} got={got}")

    print("\nRules were authored with the gold labels visible, so their number is "
          "in-sample.\nR7B's few-shot examples appear nowhere in the eval set, so the "
          "comparison is\nasymmetric in the rules' favour. ADR-0012 says so.")
    return 0 if all(board[arm]["gate_ok"] for arm in ("rules", "r7b")) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--question", help="route one question with both routers")
    parser.add_argument("--eval", action="store_true", help="the full table over the eval set")
    parser.add_argument(
        "--refresh", action="store_true",
        help=f"re-run R7B live and rewrite {ARTIFACT.name} (needs an API key, ~$0.0005)",
    )
    args = parser.parse_args()

    if not args.question and not args.eval:
        parser.error("pass --question or --eval")

    if args.question:
        rules_result = route_by_rules(args.question)
        linked = ", ".join(e.canonical_name for e in rules_result.linked or [])
        print(f"rules  {rules_result.route:7} via {rules_result.rule}  "
              f"({rules_result.latency_ms:.1f} ms, $0.00)")
        print(f"       linked: {linked or '-'}")
        try:
            model_result = route_by_model(args.question)
        except RouterError as exc:
            print(f"r7b    unavailable: {exc}")
        else:
            cost = (f"${model_result.cost_usd:.8f}"
                    if model_result.cost_usd is not None else "unpriced")
            print(f"r7b    {str(model_result.route):7} raw={model_result.raw!r}  "
                  f"({model_result.latency_ms:.0f} ms, {cost})")
        return 0

    rows = load_questions()
    if args.refresh:
        run_id = decision_log.new_run_id()
        print(f"sweeping {len(rows)} questions x 2 routers, run_id={run_id}", file=sys.stderr)
        artifact = sweep(rows, run_id)
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in artifact) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {ARTIFACT}", file=sys.stderr)
    else:
        artifact = load_artifact()

    return _report(rows, artifact)


if __name__ == "__main__":
    raise SystemExit(main())
