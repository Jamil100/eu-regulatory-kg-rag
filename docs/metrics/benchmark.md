# Benchmark metrics — the three-way comparison

Status: **BLOCKED — 1 of 4 replay arms complete. The Cohere Trial key's 1,000-call
monthly quota was exhausted mid-run on 2026-08-16.**

`vector` is swept and committed (100 rows, 0 errors, 0 ungraded, 2 truncated).
`rerank` died 12 rows in; `hybrid` and `hybrid-oracle` never started; the live
pass has not run. **No benchmark number is published below and none should be
quoted from a single arm** — a one-arm table has no comparison in it, which is
the entire point of the exercise.

The failure is a quota, not throttling: the 429 response carried
`x-trial-endpoint-call-remaining: 14` of a 20/minute allowance, so backoff had
nothing to wait for. `tenacity` retried six times against a monthly cap and could
not clear it. Resuming needs a production key (or the monthly reset); the harness
appends per arm and per mode, so `--refresh --system rerank` picks up exactly
where this stopped without re-running `vector`.

Fifth companion to `extraction-cost-and-findings.md` (what came out of the model),
`graph-load.md` (what came out of the loader), `eval-set.md` (the instrument) and
`query-path.md` / `answer-path.md` (the pipeline). This one measures **the claim
the repository exists to make**: that a hybrid of graph and vector retrieval beats
either alone on questions requiring more than one hop.

Regenerate with:

```bash
python -m eval.run_benchmark --eval        # every number below, from the artifact, $0.00
python -m eval.run_benchmark --markdown    # just the README table
```

Every figure here is recomputed by a pure `scoreboard()` from `eval/benchmark.jsonl`
with no database, no API key and no network — the same guarantee the other four
harnesses make, and the reason `--eval` works with the containers down.

---

## What is being compared

| System | Retrieval | Route |
|---|---|---|
| `vector` | raw HNSW draw, top-5, **no reranker** | forced `vector` |
| `rerank` | same draw, reordered by Rerank 3.5, top-5 | forced `vector` |
| `hybrid` | graph statements + reranked passages | **adopted rules router** |
| `hybrid-oracle` | same as `hybrid` | the eval set's hand-verified gold route |

The two baselines are forced to `vector` because they *are* the vector baselines —
letting the router send a baseline row to `graph` would turn the comparison into a
comparison of routers.

`hybrid-oracle` is a **ceiling, not a deployable system**: it uses route labels a
live request does not have. It exists because the router was re-measured at 70/99
on this eval set, so a weak `hybrid` cell otherwise confounds *"the graph path
could not answer"* with *"the router never asked it to"*. The gap between the two
hybrid rows is the router's cost, reported as a number. See ADR-0015 and the
ADR-0012 amendment.

## The protocol, pre-registered before any money was spent

Recorded in full in `eval/run_benchmark.py`'s docstring and in ADR-0015; the four
rules that matter:

1. **Accuracy from a replayed pass, latency and cost from a separate live pass.**
   Replay gives all four systems byte-identical passages, so an accuracy
   difference is the system and not a different vector draw — but a replayed row
   never pays the embed or rerank round trip, so its cost and latency are fiction.
   Two passes, `mode` on every row, `scoreboard()` keeps them apart.
2. **Per-system p95 is published; per-stratum p95 is not.** A stratum carries 5–20
   rows and a p95 over 5 is the maximum wearing a percentile's name.
3. **The three refusal strata are never averaged.** They have three different
   correct behaviours and two are exact opposites.
4. **`expected_fail` rows go in their own bucket** — neither passes nor failures.

A fifth rule is imposed by `scoreboard()` rather than chosen: **the systems do not
share a denominator unless one is forced on them.** Each drops its own errored and
`MAX_TOKENS` rows and they are not the same rows, so the per-system accuracy and
the `common` accuracy are both published and only `common` is comparable.

## What the retrieval ceiling already predicted

Measured before the benchmark ran, in `eval-set.md` and `tests/test_reranker.py`:

**46 of 203 gold references (22.7%) are not in the 50-candidate pool at all**, so
no reranker and no `k ≤ 50` can reach them. The loss is not uniform:

| Stratum | Gold unreachable at k≤50 |
|---|---|
| single-hop | **0.0%** |
| hard-negative | **0.0%** |
| two-hop | 16.7% |
| three-hop | 25.0% |
| cross-regulation | 34.4% |
| aggregation | 35.4% |

This is the hybrid thesis as a retrieval fact rather than an argument, and it sets
an expectation the table can be checked against: the two vector arms should be at
parity with the hybrid on single-hop and hard-negative, and should fall away
exactly where the pool stops containing the answer.

**Three rows lose 100% of their gold to the vector path** — `ag-008`, `xr-007`,
`th-004` — so the two baselines cannot ground them at all, whatever the generator
does.

## Two rows defeat both halves independently

Worth watching in any per-row reading of the results:

- **`xr-007`** ("Does the AI Act override the GDPR?") — all its gold is outside the
  vector candidate pool, **and** it is the single row where the rules selector
  falls short of the template oracle (61 vs 62), because it anchors
  `cross_regulation` on the wrong entity.
- **`ag-006`** (AI Act fine tiers) — labelled `graph` on the argument that the four
  Art. 99 paragraphs are lexically near-identical and defeat embedding retrieval;
  the selector then fires no rule at all for it (`S0-none`).

## The budget the hybrid runs under

`ADOPTED_BUDGET = "first"`, `DEFAULT_BUDGET_N = 50` (ADR-0014). Re-measured on this
eval set, that budget **retains 20 of the 61 gold chunks the graph path can
reach**; `uncapped` reaches all 61 and `anchor` reaches 26.

The adoption still stands on its original grounds — `first` was the only arm that
answered every row without running to `MAX_TOKENS`, and a budget that retains more
gold and then truncates retains nothing. But 33 of 52 routed rows now carry more
than 50 statements, where at 23 rows none did, so the cost of the choice is four
times more visible than when it was made. **If the hybrid underperforms, read this
before reading the router.**

---

## Results

_TBD — populated from `python -m eval.run_benchmark --eval` once the four replay
arms and the live pass have completed._

## Judge agreement

_TBD — `python -m eval.judge --agreement`, against a hand-graded 20% holdout
written blind via `python -m eval.grade_holdout`._

## Open

_TBD._
