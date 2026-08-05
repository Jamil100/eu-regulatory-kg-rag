"""What `POST /ask` costs and how long it takes, per route, measured live.

Step 6's per-route table (`docs/metrics/answer-path.md:343-347`) is a *sweep*
table, and it says so: the vector half is replayed from `rerank-eval.jsonl` at
$0.00, so `graph`-routed rows show none of the embed+rerank round trip a real
request pays, and no row shows routing, connection checkout, serialisation or
the decision-log write. This module pays all of it and reports the difference.

WHAT IT MEASURES, AND WHAT IT CANNOT.

`sweep()` drives the app through `fastapi.testclient.TestClient`, so the lifespan
runs, the pool is a pool, and the number recorded is the handler's own wall
clock. `AskResponse` has five fields, so this artifact sees the API and nothing
behind it: no document counts, no gold retention, no citation diagnostics. That
is the split on purpose -- `answer-eval.jsonl` measures the pipeline and
`ask-eval.jsonl` measures the interface -- and it is why `wall_ms` (what the
client observed) is recorded beside `latency_ms` (what the server reported).
The gap between them is serialisation plus the test transport.

THE REPORTING RULE IS PRE-REGISTERED, BECAUSE THE ns ARE SMALL.

The router sends 13 rows to `vector`, 9 to `both` and 1 to `graph`. A per-route
p95 over 9 observations is the maximum wearing a percentile's name, and over 1
it is the observation itself. So: **p50 and min-max per route, and a single
pooled p95 over all rows, labelled as pooled.** This is the same refusal
`query-path.md:429-434` already makes for the rerank tail, written down here
before any money was spent rather than after seeing which framing flattered the
result.

Costs about $0.20 to refresh. Every number in the metrics doc is recomputed from
the committed artifact by a pure `scoreboard()`, so `--eval` needs no container,
no key and no spend.

    python -m src.api.ask_eval --eval              # the table, from the artifact
    python -m src.api.ask_eval --eval --refresh    # re-run live (~$0.20)
    python -m src.api.ask_eval --question "..."    # one question through /ask
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

__all__ = ["ARTIFACT", "load_artifact", "main", "scoreboard", "sweep"]

ROOT = Path(__file__).resolve().parents[2]

# Beside the eval set it measures, and tracked, for the reason `router.py:66`
# gives: the tests and the metrics doc read their numbers out of it and both must
# work with no API key and no spend. `data/` is gitignored wholesale, which is
# why the decision log is not this file.
ARTIFACT = ROOT / "eval" / "ask-eval.jsonl"

# The routes, in the order the table prints them.
ROUTE_ORDER = ("vector", "both", "graph")


def load_questions() -> list[dict]:
    from src.query.router import QUESTIONS

    return [
        json.loads(line)
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------
# The sweep -- live. Needs containers and a key.
# --------------------------------------------------------------------------

def sweep(rows: list[dict]) -> list[dict]:
    """Drive every question through the real handler, once.

    Through `TestClient` rather than `answer()` directly, because the thing being
    measured is the endpoint: a call straight to `answer()` would miss the
    routing, the pool checkout and the log write, which is exactly the gap this
    module exists to close.
    """
    from fastapi.testclient import TestClient

    from src.api.app import app

    out: list[dict] = []
    with TestClient(app) as client:
        health = client.get("/health").json()
        if health["status"] != "ok":
            raise SystemExit(f"app is degraded, refusing to sweep: {health}")

        for row in rows:
            started = time.perf_counter()
            response = client.post("/ask", json={"question": row["question"]})
            wall_ms = (time.perf_counter() - started) * 1000

            record: dict[str, Any] = {
                "question_id": row.get("id"),
                "question": row["question"],
                "stratum": row.get("stratum"),
                "gold_route": row.get("route"),
                "expected_fail": bool(row.get("expected_fail")),
                "status_code": response.status_code,
                "wall_ms": wall_ms,
            }
            if response.status_code == 200:
                body = response.json()
                record.update({
                    "route": body["route"],
                    "latency_ms": body["latency_ms"],
                    "cost_usd": body["cost_usd"],
                    "n_citations": len(body["citations"]),
                    "answer_chars": len(body["answer"]),
                    "labels": sorted({c["citation_label"] for c in body["citations"]}),
                    "sources": sorted({c["source"] for c in body["citations"]}),
                    "error": None,
                })
            else:
                record.update({
                    "route": None,
                    "latency_ms": None,
                    "cost_usd": None,
                    "n_citations": 0,
                    "answer_chars": 0,
                    "labels": [],
                    "sources": [],
                    "error": response.json().get("detail"),
                })
            print(
                f"  {record['question_id']:<8} {record['route']!s:<7} "
                f"{record['status_code']} {wall_ms:>8.0f} ms "
                f"${record['cost_usd'] or 0:.4f} {record['n_citations']} cited",
                file=sys.stderr,
            )
            out.append(record)
    return out


# --------------------------------------------------------------------------
# Measurement -- PURE. No DB, no key, no spend.
# --------------------------------------------------------------------------

def load_artifact(path: Path | None = None) -> list[dict]:
    path = path or ARTIFACT
    if not path.exists():
        raise SystemExit(
            f"{path} does not exist. Run --eval --refresh with containers up and "
            f"an API key (~$0.20)."
        )
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _p(values: list[float], q: float) -> float | None:
    """The q-th percentile by nearest rank. None on an empty list.

    Nearest rank rather than interpolation: an interpolated p95 over 23 samples
    invents a value that no request ever took, and the point of this file is to
    report what was observed.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * len(ordered) + 0.5) - 1))
    return ordered[index]


def scoreboard(artifact: list[dict]) -> dict[str, Any]:
    """Every published number, recomputed from the artifact alone.

    Pure. That is what lets `tests/test_api.py` and
    `docs/metrics/answer-path.md` quote the same figures, and what makes
    `--eval` reproducible with the containers down.

    Per-route p95 is deliberately **not** computed. See the module docstring:
    the ns are 13 / 9 / 1 and a p95 over 9 is a maximum with a better name. The
    pooled p95 is computed once, over every served row, and is labelled `pooled`
    in the returned dict so a reader cannot pick it up as a per-route figure.
    """
    served = [row for row in artifact if row.get("status_code") == 200]
    failed = [row for row in artifact if row.get("status_code") != 200]

    per_route: dict[str, dict[str, Any]] = {}
    for route in ROUTE_ORDER:
        rows = [row for row in served if row.get("route") == route]
        if not rows:
            continue
        latencies = [row["latency_ms"] for row in rows]
        costs = [row["cost_usd"] for row in rows if row.get("cost_usd") is not None]
        per_route[route] = {
            "n": len(rows),
            "latency_p50": statistics.median(latencies),
            "latency_min": min(latencies),
            "latency_max": max(latencies),
            "cost_median": statistics.median(costs) if costs else None,
            "cost_min": min(costs) if costs else None,
            "cost_max": max(costs) if costs else None,
            "cost_total": sum(costs) if costs else None,
            "unpriced": len(rows) - len(costs),
            "citations_median": statistics.median([row["n_citations"] for row in rows]),
            "uncited_rows": sum(1 for row in rows if row["n_citations"] == 0),
        }

    all_latencies = [row["latency_ms"] for row in served]
    all_costs = [row["cost_usd"] for row in served if row.get("cost_usd") is not None]

    # What the client saw minus what the server reported: transport plus
    # serialisation. Reported so the two columns in the artifact are not read as
    # a disagreement about the same quantity.
    overheads = [
        row["wall_ms"] - row["latency_ms"]
        for row in served
        if row.get("wall_ms") is not None and row.get("latency_ms") is not None
    ]

    return {
        "rows": len(artifact),
        "served": len(served),
        "failed": [
            {"question_id": row.get("question_id"), "status": row.get("status_code"),
             "error": row.get("error")}
            for row in failed
        ],
        "per_route": per_route,
        "pooled": {
            "latency_p50": statistics.median(all_latencies) if all_latencies else None,
            "latency_p95": _p(all_latencies, 0.95),
            "latency_max": max(all_latencies) if all_latencies else None,
            "cost_total": sum(all_costs) if all_costs else None,
            "cost_mean": statistics.mean(all_costs) if all_costs else None,
            "unpriced": len(served) - len(all_costs),
        },
        "transport_overhead_ms_p50": statistics.median(overheads) if overheads else None,
        "route_agreement": {
            "n": sum(1 for row in served if row.get("gold_route")),
            "matches": sum(
                1 for row in served
                if row.get("gold_route") and row["route"] == row["gold_route"]
            ),
        },
    }


def _report(board: dict[str, Any]) -> int:
    print(f"\nrows {board['rows']}  served {board['served']}  failed {len(board['failed'])}")
    for failure in board["failed"]:
        print(f"  FAILED {failure['question_id']}: {failure['status']} {failure['error']}")

    print("\nPer route (p50 and min-max; no per-route p95 -- see the module docstring)")
    print(f"{'route':<8}{'n':>3}  {'latency p50':>12}  {'latency min-max':>22}  "
          f"{'cost median':>12}  {'cost min-max':>22}")
    for route, stats in board["per_route"].items():
        span = f"{stats['latency_min']:,.0f}-{stats['latency_max']:,.0f} ms"
        cost_span = (
            f"${stats['cost_min']:.4f}-${stats['cost_max']:.4f}"
            if stats["cost_min"] is not None else "unpriced"
        )
        median = f"${stats['cost_median']:.4f}" if stats["cost_median"] is not None else "unpriced"
        print(f"{route:<8}{stats['n']:>3}  {stats['latency_p50']:>9,.0f} ms  "
              f"{span:>22}  {median:>12}  {cost_span:>22}")

    pooled = board["pooled"]
    if pooled["latency_p50"] is not None:
        print(f"\nPooled over all served rows: p50 {pooled['latency_p50']:,.0f} ms, "
              f"p95 {pooled['latency_p95']:,.0f} ms, max {pooled['latency_max']:,.0f} ms")
        print(f"Cost: ${pooled['cost_total']:.4f} total, ${pooled['cost_mean']:.4f} mean per question, "
              f"{pooled['unpriced']} unpriced")
    if board["transport_overhead_ms_p50"] is not None:
        print(f"Client-observed minus server-reported, p50: "
              f"{board['transport_overhead_ms_p50']:.1f} ms")

    agreement = board["route_agreement"]
    if agreement["n"]:
        print(f"Route agreement with the gold labels: {agreement['matches']} of {agreement['n']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--question", help="one question through the real handler")
    parser.add_argument("--eval", action="store_true", help="the table, from the artifact")
    parser.add_argument(
        "--refresh", action="store_true",
        help=f"re-run the sweep live and rewrite {ARTIFACT.name} (needs containers and a key, ~$0.20)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not (args.question or args.eval):
        parser.error("pass --question or --eval")

    if args.question:
        from fastapi.testclient import TestClient

        from src.api.app import app

        with TestClient(app) as client:
            response = client.post("/ask", json={"question": args.question})
        if args.json:
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
            return 0 if response.status_code == 200 else 1
        if response.status_code != 200:
            print(f"{response.status_code}: {response.json().get('detail')}")
            return 1
        body = response.json()
        cost = f"${body['cost_usd']:.4f}" if body["cost_usd"] is not None else "cost unknown"
        print(f"\nroute {body['route']}  {body['latency_ms']:,.0f} ms  {cost}")
        print(f"\n{body['answer']}\n")
        for cited in body["citations"]:
            print(f"  {cited['citation_label']:<26} [{cited['source']}] "
                  f"\"{cited['text'][:60]}\"")
        return 0

    if args.refresh:
        print(f"sweeping {ARTIFACT.name} live over the eval set (~$0.20)", file=sys.stderr)
        fresh = sweep(load_questions())
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in fresh) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {len(fresh)} rows to {ARTIFACT}", file=sys.stderr)

    board = scoreboard(load_artifact())
    if args.json:
        print(json.dumps(board, ensure_ascii=False, indent=2, default=str))
        return 0
    return _report(board)


if __name__ == "__main__":
    raise SystemExit(main())
