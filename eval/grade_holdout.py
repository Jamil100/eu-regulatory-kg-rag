"""Print the holdout answers with the judge's verdicts STRIPPED, for hand grading.

Roadmap S5.3 asks for a hand-verified 20% sample and a reported agreement figure.
An agreement figure is only worth reporting if the hand labels were produced
without seeing the machine ones -- otherwise it measures anchoring, not agreement.

The eval set cannot supply that discipline on its own, because the answers being
graded do not exist until the benchmark has run and the benchmark grades as it
goes. So the two are separated mechanically instead:

  1. `python -m eval.judge --holdout`      picks WHICH 20 rows, from the eval set
                                           alone, before any answer exists
  2. `python -m eval.grade_holdout`        prints those rows' question, gold,
                                           grading rule and ANSWER -- and refuses
                                           to print `verdict`, `judge_reason`,
                                           `judge_defect` or `capped_from`
  3. hand labels are written to            eval/judge-agreement.jsonl
  4. `python -m eval.judge --agreement`    compares the two, names disagreements

Step 2 is the load-bearing one and it is why this is a module rather than a
one-line `jq`. `test_grade_holdout_never_prints_a_verdict` asserts the omission,
so the blind half of the protocol is enforced by the suite rather than by the
good intentions of whoever runs it.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

# Fields that carry the machine's opinion. Printing any of these would defeat the
# entire point of the module.
WITHHELD = ("verdict", "judge_reason", "judge_defect", "judge_capped_from", "judge_error")


def blind_rows(artifact: list[dict], ids: list[str], system: str) -> list[dict]:
    """The holdout rows for one system, with every judgement field removed.

    Returns plain dicts rather than printing, so the test can assert on the
    payload instead of scraping stdout.
    """
    wanted = set(ids)
    out = []
    for row in artifact:
        if row.get("system") != system or row.get("mode") != "replay":
            continue
        if row["id"] not in wanted:
            continue
        out.append({k: v for k, v in row.items() if k not in WITHHELD})
    return sorted(out, key=lambda r: r["id"])


def main() -> int:
    from eval.judge import AGREEMENT_PATH, VERDICTS, holdout
    from eval.run_benchmark import DEPLOYED_SYSTEM, load_artifact, load_questions

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--system", default=DEPLOYED_SYSTEM,
        help="which system's answers to grade (default: the deployed hybrid)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    questions = {r["id"]: r for r in load_questions()}
    ids = holdout(load_questions())
    rows = blind_rows(load_artifact(), ids, args.system)
    if not rows:
        raise SystemExit(
            f"no replayed rows for system {args.system!r} in the artifact -- run "
            f"`python -m eval.run_benchmark --refresh --system {args.system}` first"
        )

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    print(f"\nHAND-GRADE THESE {len(rows)} ANSWERS FROM SYSTEM {args.system!r}.")
    print(f"The judge's verdicts are deliberately withheld. Write one object per")
    print(f"line to {AGREEMENT_PATH.name}:")
    print('  {"id": "sh-001", "verdict": "correct"}')
    print(f"Valid verdicts: {', '.join(VERDICTS)}")

    for row in rows:
        q: dict[str, Any] = questions[row["id"]]
        print("\n" + "=" * 78)
        print(f"{row['id']}  [{q['stratum']}]  must_cite={q['must_cite']}  "
              f"route={row.get('route')}  citations={len(row.get('cited_chunk_ids') or [])}")
        print("-" * 78)
        print(f"QUESTION\n  {q['question']}")
        print(f"\nGOLD\n  {q['gold']}")
        print(f"\nGRADING RULE (decisive)\n  {q['grading_rule']}")
        print(f"\nANSWER UNDER TEST\n  {(row.get('answer') or '(no answer produced)')}")
        print(f"\nCITED  {', '.join(row.get('cited_chunk_ids') or []) or '(nothing)'}")
        print(f"GOLD CITED  {', '.join(row.get('gold_cited') or []) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
