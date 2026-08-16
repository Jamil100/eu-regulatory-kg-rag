"""LLM judge + agreement check. Command A, temperature 0.

Grades one answer against its gold as `correct` / `partially_correct` / `wrong` /
`correct_refusal`. This is the module every accuracy cell in the README benchmark
table is computed from, so it is documented to the same standard as the retrieval
path -- a benchmark is only as trustworthy as its grader.

THE SIGNATURE TAKES THE WHOLE ROW, AND THAT IS THE POINT OF THIS REWRITE.

The stub was `judge(question, gold, answer)`. Every eval row carries a
`grading_rule` -- often several sentences -- that encodes the partial/wrong
boundary for that specific question ("omitting 'whichever is higher' is partial",
"dropping 'solely' is wrong, not partial, because it changes which systems are in
scope"). Grading without it means the judge invents its own boundary per row and
the published accuracy is a measurement of the judge's mood. The row also carries
`stratum` and `must_cite`, which decide what "correct" even means here.
`plan-phase-3-router-and-query-path.md:781-784` named the narrow signature as a
Phase 5 blocker; this is that widening.

THE MECHANICAL HALF IS NOT ASKED OF THE MODEL.

Whether an answer carries a citation is a fact about a list, not a judgement, and
`citation_defect()` computes it in Python. The three refusal modes want three
different things from that fact:

    out-of-scope   refuse, name the scope limit, cite NOTHING -> any citation is a defect
    unanswerable   refuse by naming the SPECIFIC absence,     -> any citation is a defect
                   cite NOTHING
    hard-negative  reject the premise and CITE the correcting -> zero citations is a defect
                   text

`docs/metrics/eval-set.md:63-67` is explicit that averaging these into one
"refusal rate" hides which behaviour failed. Asking a language model to count
citations and then apply a per-stratum convention to the count is two chances to
be wrong about something `len()` already knows. The model is told the outcome and
judges the prose.

THE VERDICT CAP IS APPLIED AFTER THE CALL, NOT REQUESTED IN THE PROMPT.

A refusal row that cites something cannot be `correct` no matter how good the
prose is, and a `must_cite` row whose correct answer carries no citation is
`partially_correct` at best -- `docs/metrics/answer-path.md` records exactly that
case. Those are deterministic consequences of `citation_defect()`, so `_cap()`
imposes them in code afterwards. A prompt instruction would be a request; this is
an invariant, and `tests/test_judge.py` pins it.

WHAT THIS MODULE DOES NOT DO.

It does not decide the `expected_fail` bucket. `run_benchmark.bucket_of()` owns
that, and rows carrying a recorded `expected_fail` reason are reported separately
rather than being counted as passes (which silences the canary) or as system
failures (which blames retrieval for an extraction gap).

    python -m eval.judge --agreement          # the agreement figure, from artifacts, $0.00
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

from src.config import price_of, settings

__all__ = [
    "AGREEMENT_PATH",
    "JUDGE_MAX_TOKENS",
    "SYSTEM_PROMPT",
    "Verdict",
    "JudgeError",
    "JudgeResult",
    "agreement",
    "build_prompt",
    "citation_defect",
    "judge",
]

ROOT = Path(__file__).resolve().parents[1]

# The hand-graded holdout, tracked beside the eval set it samples. Roadmap S5.3
# asks for a hand-verified 20% sample and a reported agreement figure; the house
# rule from `docs/failure-notes.md` is that a verification not in the code is a
# memory, so the hand labels live in a file a test reads.
AGREEMENT_PATH = ROOT / "eval" / "judge-agreement.jsonl"

Verdict = Literal["correct", "partially_correct", "wrong", "correct_refusal"]

VERDICTS: tuple[str, ...] = get_args(Verdict)

# A verdict plus a sentence of reasoning. Not 800 like `generate.MAX_TOKENS`:
# this call produces a label, and a judge given room to write an essay writes one
# and then agrees with itself.
JUDGE_MAX_TOKENS = 300

# Strata whose correct behaviour is a refusal that cites NOTHING. `hard-negative`
# is deliberately absent -- it is a refusal that MUST cite.
REFUSE_UNCITED = ("out-of-scope", "unanswerable")

SYSTEM_PROMPT = """\
You grade answers about the EU AI Act (Regulation (EU) 2024/1689) and the GDPR \
(Regulation (EU) 2016/679) against a gold answer written from the legislative \
text by a human.

You will be given the question, the gold answer, a grading rule specific to this \
question, and the answer under test. Return one verdict.

The four verdicts:

- `correct` -- the answer conveys what the gold conveys on every point the \
grading rule treats as required. Wording need not match; legal substance must.
- `partially_correct` -- the answer is right as far as it goes but omits \
something the grading rule requires, or states a rule without an exception that \
the gold states.
- `wrong` -- the answer contradicts the gold, states a figure, tier, deadline or \
provision that the gold does not support, or accepts a premise the gold rejects.
- `correct_refusal` -- the gold itself is a refusal (it begins with REFUSE) and \
the answer also declines for substantially the reason the gold gives.

Rules, in order of precedence:

1. THE GRADING RULE GOVERNS. It is written for this question and it overrides \
your own sense of how serious an omission is. Where it says an omission is \
partial, that is partial; where it says an omission is wrong, that is wrong.
2. Grade legal substance, not style, length or citation formatting.
3. A confident answer that supplies a figure the gold does not contain is \
`wrong`, not `partially_correct`. Fabrication is the failure this benchmark \
exists to detect and it never scores as a near miss.
4. Where the gold is a refusal because the text states no such fact, an answer \
that supplies the fact is `wrong` however plausible it sounds.
5. Do not reward agreement with the questioner. Where the gold rejects the \
question's premise, an answer that accepts the premise is `wrong` even if \
everything after it is accurate.

Reply with JSON only, no prose around it:
{"verdict": "<one of correct|partially_correct|wrong|correct_refusal>", \
"reason": "<one sentence, naming the specific point that decided it>"}"""


class JudgeError(RuntimeError):
    """An answer could not be graded.

    Not `SystemExit`, following `RouterError` (src/query/router.py:101) and
    `GenerateError` -- a benchmark sweep must record a failed grade as this row's
    result rather than take the whole run down. `run_benchmark` catches it.
    """


@dataclass
class JudgeResult:
    """One grading call, with everything the artifact needs to be re-derivable."""

    verdict: Verdict | str = ""
    reason: str = ""
    # What `citation_defect()` found, or "" -- carried so the artifact records
    # WHY a verdict was capped rather than leaving a reader to re-derive it.
    defect: str = ""
    capped_from: str = ""
    raw: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float = 0.0
    attempts: int = 1
    dropped: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# The mechanical half -- PURE. No model, no key, no spend.
# --------------------------------------------------------------------------

def citation_defect(row: dict, citations: Sequence[Any]) -> str:
    """The per-stratum citation convention, checked in Python. "" if clean.

    `citations` is any sequence of `Citation`-shaped objects or dicts; only its
    length is read, because that is the whole of the convention. Kept
    duck-typed so a test can pass `[]` and `[{}]` without constructing models.

    The three modes differ and the difference is the measurement, so this returns
    a *named* defect rather than a bool: "cited on a no-citation row" and
    "uncited on a must-cite row" are opposite failures and a single boolean would
    report a system that made both as consistent.
    """
    n = len(citations)
    if row.get("stratum") in REFUSE_UNCITED:
        if n:
            return f"cited {n} source(s) on a {row['stratum']} row, which must cite nothing"
        return ""
    if row.get("must_cite") and n == 0:
        return "produced no citation on a must_cite row"
    return ""


def _cap(verdict: str, defect: str, row: dict) -> str:
    """Impose the consequences of a citation defect on the verdict.

    Deterministic, and applied after the call rather than asked for in the
    prompt, because these are invariants rather than judgements:

    - On an `out-of-scope` / `unanswerable` row, a citation means the system did
      not do the thing the row measures. `correct_refusal` degrades to
      `partially_correct`; the eval rows say so in their own grading rules
      ("A refusal that names the scope limit but adds a citation is at most
      partial" -- oos-002).
    - On a `must_cite` row, a correct answer with nothing attached is exactly the
      case `docs/metrics/answer-path.md` records: right prose, no grounding.
      `correct` degrades to `partially_correct`.

    A verdict that is already `wrong` stays `wrong` -- a defect cannot improve it.
    """
    if not defect or verdict == "wrong":
        return verdict
    if verdict in ("correct", "correct_refusal"):
        return "partially_correct"
    return verdict


def _parse(raw: str) -> tuple[str, str, list[str]]:
    """Pull the verdict and reason out of the model's reply.

    Tolerant of a fenced code block and of prose around the JSON, because a
    grader that raises on formatting throws away a paid call. An unparseable
    reply is a `JudgeError` -- silently defaulting to a verdict would put a
    fabricated grade into the artifact, which is worse than a recorded failure.
    """
    dropped: list[str] = []
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    brace = re.search(r"\{.*\}", text, re.S)
    if brace:
        text = brace.group(0)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge reply was not JSON: {raw[:200]!r} ({exc})") from exc

    verdict = str(payload.get("verdict", "")).strip().lower().replace(" ", "_").replace("-", "_")
    if verdict not in VERDICTS:
        # Recorded and raised, never coerced. A near-miss label ("partial") that
        # this quietly mapped to `partially_correct` would make the judge's
        # output format unfalsifiable.
        raise JudgeError(f"judge returned an unknown verdict {verdict!r}; have {list(VERDICTS)}")
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        dropped.append("reply carried no reason")
    return verdict, reason, dropped


def build_prompt(row: dict, answer: str, defect: str) -> list[dict[str, str]]:
    """System prompt plus one user turn carrying the whole grading context.

    The grading rule is labelled as decisive in the user turn as well as in the
    system prompt. It is the field that makes two graders agree, and burying it
    under the gold answer is how it gets skimmed.
    """
    parts = [
        f"QUESTION\n{row['question']}",
        f"\nGOLD ANSWER (written from the legislative text by a human)\n{row['gold']}",
        f"\nGRADING RULE FOR THIS QUESTION -- this is decisive\n{row['grading_rule']}",
        f"\nANSWER UNDER TEST\n{answer.strip() or '(the system produced no answer)'}",
    ]
    if defect:
        parts.append(
            f"\nCITATION CHECK (already established -- do not re-derive)\n{defect}. "
            f"Grade the substance of the prose; the consequence of this defect is "
            f"applied separately."
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


# --------------------------------------------------------------------------
# The grading call -- impure. Needs a key.
# --------------------------------------------------------------------------

def get_client() -> Any:
    """A Cohere client that raises `JudgeError` rather than exiting.

    Same split as `generate.get_client()` (src/answer/generate.py:166) and
    `router.get_client()`: `embedder.get_client()` raises `SystemExit`, which is
    right for a CLI and wrong inside a sweep that must record the failure.
    """
    import cohere

    if not settings.cohere_api_key:
        raise JudgeError(f"{settings.cohere_api_key_var} is not set")
    return cohere.ClientV2(api_key=settings.cohere_api_key)


def _chat_call(client: Any, messages: list[dict]) -> tuple[Any, int]:
    """The retrying unit, mirroring `generate._chat_call`.

    Written out again rather than imported for the reason that module records:
    three chat call sites shipped without a retry and two were bitten by a
    10-calls/minute trial key, once scoring a row zero one step after the lesson
    was written down. A 100-row benchmark grades 300 answers; without this it
    would not finish.
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
            model=settings.model_generate,
            messages=messages,
            # Explicit, never defaulted -- same reasoning as generate.py's
            # citation_options note. A grader whose temperature drifts upstream
            # silently re-measures every accuracy cell in the README.
            temperature=0,
            max_tokens=JUDGE_MAX_TOKENS,
        )

    response = _call()
    return response, _call.retry.statistics.get("attempt_number", 1)


def judge(
    row: dict,
    answer: str,
    citations: Sequence[Any] = (),
    client: Any | None = None,
) -> JudgeResult:
    """Grade one answer against its gold. One call, one verdict.

    `row` is a whole eval-set row -- it must carry `question`, `gold`,
    `grading_rule`, `stratum` and `must_cite`. See the module docstring for why
    the narrow `(question, gold, answer)` signature was retired.
    """
    for required in ("question", "gold", "grading_rule"):
        if not str(row.get(required, "")).strip():
            raise JudgeError(f"row {row.get('id')!r} has no {required}")

    defect = citation_defect(row, citations)
    client = client or get_client()
    started = time.perf_counter()
    try:
        response, attempts = _chat_call(client, build_prompt(row, answer, defect))
    except Exception as exc:  # noqa: BLE001 -- the SDK raises many shapes; all are one outcome here
        raise JudgeError(f"grading {row.get('id')!r} failed: {exc}") from exc
    latency_ms = (time.perf_counter() - started) * 1000

    raw = "".join(item.text for item in response.message.content if getattr(item, "text", None))
    verdict, reason, dropped = _parse(raw)
    capped = _cap(verdict, defect, row)

    # Billed units first, `usage.tokens` as the fallback -- the two differ
    # whenever a request hits the inference cache, and billed units are what you
    # are charged. Same order as generate.py and template_selector.py.
    usage = getattr(response, "usage", None)
    units = getattr(usage, "billed_units", None) or getattr(usage, "tokens", None)
    in_tok = int(getattr(units, "input_tokens", 0) or 0)
    out_tok = int(getattr(units, "output_tokens", 0) or 0)

    return JudgeResult(
        verdict=capped,
        reason=reason,
        defect=defect,
        capped_from=verdict if capped != verdict else "",
        raw=raw,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=price_of(settings.model_generate, in_tok, out_tok),
        latency_ms=latency_ms,
        attempts=attempts,
        dropped=dropped,
    )


# --------------------------------------------------------------------------
# Agreement -- PURE. No model, no key, no spend.
# --------------------------------------------------------------------------

def agreement(judged: dict[str, str], hand: dict[str, str]) -> dict[str, Any]:
    """Raw agreement between the judge and the hand-graded holdout.

    Raw agreement, not kappa, and the choice is deliberate: kappa needs a
    marginal distribution to correct against, and over a 20-row stratified
    holdout with four labels those marginals are themselves noise. The number
    published is the one that was measured, with the disagreement rows named
    individually so a reader can see WHICH labels moved rather than only how
    many -- a 90% that disagrees on both refusal rows is a worse grader than a
    90% that disagrees on two single-hop rows.

    Both arguments are `{row_id: verdict}`. Only ids present in `hand` are
    scored; a hand label with no matching judgement is reported in `missing`
    rather than dropped, because a silently shrinking denominator is how an
    agreement figure flatters itself.
    """
    scored = {rid: v for rid, v in hand.items() if rid in judged}
    missing = sorted(set(hand) - set(judged))
    matches = sorted(rid for rid, v in scored.items() if judged[rid] == v)
    disagreements = [
        {"id": rid, "hand": v, "judge": judged[rid]}
        for rid, v in sorted(scored.items())
        if judged[rid] != v
    ]
    return {
        "n": len(scored),
        "matches": len(matches),
        "rate": (len(matches) / len(scored)) if scored else None,
        "disagreements": disagreements,
        "missing": missing,
    }


def holdout(rows: list[dict], fraction: float = 0.20) -> list[str]:
    """The stratified sample to hand-grade, chosen deterministically.

    Roadmap §5.3 asks for a hand-verified 20% sample. WHICH 20% has to be decided
    before anyone reads a verdict, or the sample becomes the rows whose grades
    looked interesting -- which is how an agreement figure is talked up after the
    fact. So this is a pure function of the eval set: no RNG, no seed to fiddle,
    no dependence on the artifact. Re-running it on the same set returns the same
    ids forever, and it can be run before the benchmark exists.

    Stratified proportionally, taking every stratum's share in `id` order, with a
    floor of one row per stratum. The floor matters: `out-of-scope` is 5 rows and
    20% of it rounds to 1, but a holdout with no refusal row in it cannot detect
    the failure mode the refusal strata exist to measure -- and the judge's
    hardest call is the `must_cite` split between `hard-negative` and the other
    two.
    """
    import collections

    by_stratum: dict[str, list[str]] = collections.defaultdict(list)
    for row in rows:
        by_stratum[row["stratum"]].append(row["id"])

    picked: list[str] = []
    for stratum in sorted(by_stratum):
        ids = sorted(by_stratum[stratum])
        take = max(1, round(len(ids) * fraction))
        # Evenly spaced through the stratum rather than the first N, so the
        # sample is not biased toward whichever rows were written first -- the
        # ids are chronological and the later ones are the 2026-08-15 batch.
        step = len(ids) / take
        picked.extend(ids[int(i * step)] for i in range(take))
    return sorted(set(picked))


def load_hand_labels(path: Path | None = None) -> dict[str, str]:
    """The hand-graded holdout, `{id: verdict}`. Raises if a verdict is unknown."""
    path = path or AGREEMENT_PATH
    if not path.exists():
        raise SystemExit(
            f"{path} does not exist. It holds the hand-graded 20% sample; see "
            f"roadmap S5.3. Grade the holdout by hand BEFORE reading any judge "
            f"verdict, then re-run."
        )
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["verdict"] not in VERDICTS:
            raise SystemExit(f"{row['id']}: unknown hand verdict {row['verdict']!r}")
        out[row["id"]] = row["verdict"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--agreement", action="store_true",
        help="report judge-vs-hand agreement from the committed artifacts ($0.00)",
    )
    parser.add_argument(
        "--holdout", action="store_true",
        help="print the stratified 20%% sample to hand-grade; deterministic, $0.00",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not (args.agreement or args.holdout):
        parser.error("pass --agreement or --holdout")

    if args.holdout:
        from eval.run_benchmark import load_questions

        rows = load_questions()
        ids = holdout(rows)
        by_id = {r["id"]: r for r in rows}
        if args.json:
            print(json.dumps(ids, indent=2))
            return 0
        print(f"\nHand-grade these {len(ids)} rows BEFORE reading any judge verdict.")
        print(f"Write one JSON object per line to {AGREEMENT_PATH.name}:")
        print('  {"id": "sh-001", "verdict": "correct"}')
        print(f"\nValid verdicts: {', '.join(VERDICTS)}\n")
        for rid in ids:
            print(f"  {rid:<10} {by_id[rid]['stratum']:<17} "
                  f"must_cite={str(by_id[rid]['must_cite']):<5} "
                  f"{by_id[rid]['question'][:58]}")
        return 0

    from eval.run_benchmark import ARTIFACT, load_artifact

    hand = load_hand_labels()
    rows = load_artifact()
    # The hybrid arm is what the hand sample was graded against; grading the
    # holdout once per system would make "agreement" ambiguous.
    #
    # `mode == "replay"` IS LOAD-BEARING AND ITS ABSENCE WAS A REAL DEFECT.
    #
    # The live pass re-answers a 30-row subsample, so a row in that subsample has
    # TWO hybrid verdicts in the artifact -- one per answer. `grade_holdout`
    # filters to `replay`, so the hand labels describe the replayed answers; this
    # comprehension did not, so the later live row silently overwrote the replay
    # verdict and the agreement figure compared a hand grade of one answer against
    # a machine grade of a different one.
    #
    # Found on the first real run: `oos-001` was the only holdout row in the live
    # sample, its replay answer was graded `wrong` and its live answer
    # `correct_refusal`, and the mismatch surfaced as a fake disagreement that
    # looked like the judge excusing a safety-critical failure. The judge had in
    # fact graded it correctly. One row of 20 is 5 percentage points of a figure
    # whose whole job is to be trusted.
    judged = {
        row["id"]: row["verdict"]
        for row in rows
        if row.get("system") == "hybrid"
        and row.get("mode") == "replay"
        and row.get("verdict")
    }
    if not judged:
        raise SystemExit(
            f"{ARTIFACT.name} holds no graded hybrid rows yet. Run "
            f"`python -m eval.run_benchmark --refresh --system hybrid` first."
        )

    board = agreement(judged, hand)
    if args.json:
        print(json.dumps(board, indent=2, ensure_ascii=False))
        return 0

    rate = f"{board['rate']:.0%}" if board["rate"] is not None else "n/a"
    print(f"\nJudge agreement with the hand-graded holdout: "
          f"{board['matches']} of {board['n']}  ({rate})")
    if board["disagreements"]:
        print("\nDisagreements -- named individually, never only counted")
        print("-" * 66)
        for row in board["disagreements"]:
            print(f"  {row['id']:<10} hand={row['hand']:<18} judge={row['judge']}")
    if board["missing"]:
        print(f"\nHand-labelled but not graded: {', '.join(board['missing'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
