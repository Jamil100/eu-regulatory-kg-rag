"""Compare two sweeps of ONE system that differ only in when they ran.

This is the E1 instrument. It exists because every accuracy comparison in this
repo is a difference between systems, and a difference between systems is only
readable against the difference a system makes with itself.

WHAT IT SEPARATES, AND WHY THAT NEEDED A HASH.

"The same system returned different text twice" has two causes with opposite
fixes:

  provider nondeterminism   we sent the same bytes and got different tokens
  a context assembly bug    we sent different bytes and did not notice

Those are indistinguishable from the answers alone, which is why
`generate._chat_call` hashes the exact dict it splats into `client.chat`
(`generate.py`, "THE BODY IS BUILT ONCE"). With the hash the two separate
cleanly:

  same sha, different text  -> the provider. Nothing in this repo can fix it;
                               every published delta needs this noise floor
                               beside it.
  different sha             -> us. The request was not what we thought it was,
                               and the arm comparison was never controlled.

REGENERATED ROWS ARE REPORTED SEPARATELY AND NOT COUNTED IN THE HASH TEST.
`answer_path.answer()` may issue a second, deliberately DIFFERENT request when
citations fail validation, and `request_sha` then names that second body. Two
runs can therefore differ in sha for a legitimate reason. Those rows are still
counted in the flip and text rates -- they are real behaviour -- but they cannot
speak to determinism, so the hash test excludes them and says how many it
dropped.

    python -m eval.repeat_report --tags e1-run-a e1-run-b
    python -m eval.repeat_report --tags e1-run-a e1-run-b --system rerank
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "eval" / "benchmark.jsonl"

PASSING = ("correct", "correct_refusal")


def load(path: Path | None = None) -> list[dict]:
    path = path or ARTIFACT
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _by_id(rows: list[dict], system: str, tag: str, mode: str = "replay") -> dict[str, dict]:
    return {
        r["id"]: r
        for r in rows
        if r.get("system") == system
        and r.get("mode") == mode
        and (r.get("run_tag") or "") == tag
    }


def compare(
    artifact: list[dict], system: str, tag_a: str, tag_b: str
) -> dict[str, Any]:
    """Every number this module reports, from the artifact alone. Pure."""
    a, b = _by_id(artifact, system, tag_a), _by_id(artifact, system, tag_b)
    shared = sorted(set(a) & set(b))

    paired = [(i, a[i], b[i]) for i in shared]
    # A row that errored in either run has no answer to compare.
    usable = [(i, x, y) for i, x, y in paired if not x.get("error") and not y.get("error")]

    text_diff = [i for i, x, y in usable if x.get("answer") != y.get("answer")]
    graded = [(i, x, y) for i, x, y in usable if x.get("verdict") and y.get("verdict")]
    verdict_flip = [i for i, x, y in graded if x["verdict"] != y["verdict"]]
    # The binary the accuracy column is actually computed on. A correct ->
    # partially_correct move is a flip; only this one moves the headline number.
    pass_flip = [
        i for i, x, y in graded
        if (x["verdict"] in PASSING) != (y["verdict"] in PASSING)
    ]

    # THE HASH TEST. Regenerated rows are excluded -- see the module docstring.
    determinism = [
        (i, x, y) for i, x, y in usable
        if not x.get("regenerated") and not y.get("regenerated")
        and x.get("request_sha") and y.get("request_sha")
    ]
    same_sha = [(i, x, y) for i, x, y in determinism if x["request_sha"] == y["request_sha"]]
    diff_sha = [(i, x, y) for i, x, y in determinism if x["request_sha"] != y["request_sha"]]
    same_sha_diff_text = [i for i, x, y in same_sha if x.get("answer") != y.get("answer")]
    same_sha_diff_verdict = [
        i for i, x, y in same_sha
        if x.get("verdict") and y.get("verdict") and x["verdict"] != y["verdict"]
    ]

    by_stratum: dict[str, dict[str, int]] = {}
    for i, x, y in graded:
        cell = by_stratum.setdefault(x["stratum"], {"n": 0, "flips": 0, "pass_flips": 0})
        cell["n"] += 1
        cell["flips"] += x["verdict"] != y["verdict"]
        cell["pass_flips"] += (x["verdict"] in PASSING) != (y["verdict"] in PASSING)

    def score(rows: dict[str, dict], ids: list[str]) -> int:
        return sum(1 for i in ids if rows[i].get("verdict") in PASSING)

    graded_ids = [i for i, _, _ in graded]
    return {
        "system": system,
        "tags": [tag_a, tag_b],
        "n_shared": len(shared),
        "n_usable": len(usable),
        "n_graded": len(graded),
        "errors": [i for i, x, y in paired if x.get("error") or y.get("error")],
        "text_diff": text_diff,
        "verdict_flip": verdict_flip,
        "pass_flip": pass_flip,
        "by_stratum": by_stratum,
        "score_a": score(a, graded_ids),
        "score_b": score(b, graded_ids),
        "regenerated_excluded": len(usable) - len(determinism),
        "n_determinism": len(determinism),
        "same_sha": len(same_sha),
        "diff_sha": [i for i, _, _ in diff_sha],
        "same_sha_diff_text": same_sha_diff_text,
        "same_sha_diff_verdict": same_sha_diff_verdict,
    }


def _pct(k: int, n: int) -> str:
    return f"{k}/{n} ({k / n:.1%})" if n else f"{k}/0 (-)"


def report(cmp: dict[str, Any]) -> int:
    a, b = cmp["tags"]
    print(f"\nREPEAT COMPARISON -- {cmp['system']!r}: {a} vs {b}")
    print("=" * 78)
    print(f"  rows in both runs        {cmp['n_shared']}")
    print(f"  comparable (no error)    {cmp['n_usable']}")
    print(f"  graded in both           {cmp['n_graded']}")
    if cmp["errors"]:
        print(f"  errored in one or both   {', '.join(cmp['errors'])}")

    print("\nTHE NOISE FLOOR")
    print("-" * 78)
    n = cmp["n_usable"]
    g = cmp["n_graded"]
    print(f"  answer text differs      {_pct(len(cmp['text_diff']), n)}")
    print(f"  verdict differs          {_pct(len(cmp['verdict_flip']), g)}")
    print(f"  PASS/FAIL bit differs    {_pct(len(cmp['pass_flip']), g)}   <- the accuracy column")
    print(f"\n  score, run {a:<12} {cmp['score_a']} of {g}")
    print(f"  score, run {b:<12} {cmp['score_b']} of {g}")
    print(f"  same system, same rows, difference of {cmp['score_b'] - cmp['score_a']:+d}")

    print("\nWAS IT US OR THE PROVIDER?")
    print("-" * 78)
    print(f"  rows testable for determinism  {cmp['n_determinism']}"
          f"   ({cmp['regenerated_excluded']} excluded: regenerated or unhashed)")
    print(f"  identical request_sha          {cmp['same_sha']}")
    if cmp["diff_sha"]:
        print(f"  DIFFERENT request_sha          {len(cmp['diff_sha'])}"
              f"  <- WE sent a different request")
        print(f"      {', '.join(cmp['diff_sha'][:20])}")
    else:
        print("  different request_sha          0")
    print(f"\n  identical body, different text     "
          f"{_pct(len(cmp['same_sha_diff_text']), cmp['same_sha'])}")
    print(f"  identical body, different verdict  "
          f"{_pct(len(cmp['same_sha_diff_verdict']), cmp['same_sha'])}")

    # THE VERDICT, stated rather than left to the reader.
    print("\n  " + "-" * 74)
    if cmp["diff_sha"]:
        print("  DIAGNOSIS: at least partly OURS. Rows above sent a different body on\n"
              "  the second run at temperature=0 -- that is a context assembly defect,\n"
              "  not the provider, and those arms were never controlled.")
    elif cmp["same_sha_diff_text"]:
        print("  DIAGNOSIS: PROVIDER NONDETERMINISM. Every comparable row sent a\n"
              "  byte-identical body and some returned different text anyway. Nothing\n"
              "  in this repo can remove it; it is a floor under every published\n"
              "  difference and must be quoted beside them.")
    else:
        print("  DIAGNOSIS: DETERMINISTIC. Identical bodies returned identical text on\n"
              "  every comparable row. A difference between arms is a real difference.")

    if cmp["by_stratum"]:
        print("\nWHERE THE FLIPS ARE")
        print("-" * 78)
        print(f"  {'stratum':<18} {'n':>3}  {'verdict flips':>14}  {'pass/fail flips':>16}")
        for stratum, cell in sorted(
            cmp["by_stratum"].items(), key=lambda kv: -kv[1]["pass_flips"]
        ):
            print(f"  {stratum:<18} {cell['n']:>3}  {cell['flips']:>14}  {cell['pass_flips']:>16}")

    if cmp["pass_flip"]:
        print(f"\n  rows whose pass/fail bit moved: {', '.join(cmp['pass_flip'])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tags", nargs=2, required=True, metavar=("TAG_A", "TAG_B"))
    parser.add_argument("--system", default="rerank")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cmp = compare(load(), args.system, args.tags[0], args.tags[1])
    if not cmp["n_shared"]:
        raise SystemExit(
            f"no rows for system={args.system!r} with tags {args.tags}. "
            f"Sweep them first with --refresh --run-tag."
        )
    if args.json:
        print(json.dumps(cmp, indent=2))
        return 0
    return report(cmp)


if __name__ == "__main__":
    raise SystemExit(main())
