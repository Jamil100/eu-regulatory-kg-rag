# Benchmark metrics — the three-way comparison

Status: **Complete.** 4 replay arms × 100 rows + 3 live arms × 30 rows, run
2026-08-16 on a production key. $4.14. Judge validated against a hand-graded 20%
sample at **85% agreement**.

**The headline is a negative result: the hybrid does not beat the vector
baselines.** It is the slowest and most expensive of the three deployable systems
and wins no accuracy column outright. Roadmap §6 Phase 5.2 anticipated this
possibility and said the debugging journey would then *be* the write-up; §Why
below is that write-up.

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

| System | Single-hop | Two-hop | Three-hop | Cross-reg | Aggregation | Refusal | p95 latency | $/query |
|---|---|---|---|---|---|---|---|---|
| Vector-only | 16/20 | 9/20 | 0/13 | 3/15 | 0/9 | 11/20 | 9.8 s (n=30) | $0.0041 |
| Vector + Rerank 3.5 | 14/19 | 6/20 | 1/14 | 5/15 | 2/7 | 12/19 | 19.2 s (n=30) | $0.0065 |
| **Hybrid (graph+vector)** | 15/20 | 5/20 | 1/14 | 5/15 | 1/7 | 8/20 | 10.9 s (n=30) | $0.0076 |
| Hybrid, gold route [ceiling] | 15/19 | 4/20 | 2/14 | 4/14 | 1/7 | 7/19 | – | – |

### The comparable denominator

The per-system columns above use each arm's own denominator and are **not**
comparable across arms — each drops its own errored and `MAX_TOKENS` rows and they
are not the same rows. Over the **89 rows every system scored**:

| System | own denominator | **common (89 rows)** | gold chunks cited |
|---|---|---|---|
| Vector-only | 39/97 | **35/89** | 85/196 |
| Vector + Rerank 3.5 | 40/94 | **37/89** | 86/180 |
| Hybrid | 35/96 | **31/89** | 83/175 |
| Hybrid, gold route | 33/93 | **30/89** | 82/171 |

The ordering is identical on either denominator, which is the useful thing about
publishing both: rerank marginally ahead, hybrid behind both baselines, the oracle
behind the hybrid.

### The router is not the explanation

| | |
|---|---|
| rows both hybrid arms scored | 93 |
| `hybrid` (adopted router) passing | 34 |
| `hybrid-oracle` (gold route) passing | 33 |
| **the router's cost** | **−1 answer** |
| rows the router sent somewhere else | 27 |
| of those, rows where it changed the answer | 4 (`3h-011`, `hn-004`, `th-010`, `th-017`) |

The router misroutes 27 of 93 rows and it costs **one answer, in the wrong
direction** — routing *more* questions to the graph was very slightly worse.
Whatever is wrong with the hybrid, `route_by_rules` is not it. That is the single
most useful thing the fourth arm bought, for about $0.90, and without it the 70/99
router accuracy would have been the obvious and wrong culprit.

## Why the hybrid lost — the number that is not in the table

**`partially_correct` is 42–46% of scored answers for every system:**

| System | correct | partial | wrong |
|---|---|---|---|
| Vector-only | 40% | 42% | 18% |
| Vector + Rerank 3.5 | 43% | 45% | **13%** |
| Hybrid | 36% | 46% | 18% |
| Hybrid, gold route | 35% | 46% | 18% |

Only `correct` counts as a pass, so ~45% of the mass sits in one excluded bucket
**identically across all four arms**, compressing every score into 35–43% and
swamping the differences between them. On three-hop every system scores exactly
8 partial / 5 wrong, whether or not it received graph statements — `hybrid` got
them on 8 of 14 three-hop rows and `hybrid-oracle` on 13 of 14, and neither moved.

The systems are not failing to *find* the law. They are failing to state it
completely: a rule without its exception, a fine tier without its SME inversion, a
chain that stops one hop short. Graph retrieval does not fix that and there is no
reason it should. This is a generation and prompting problem wearing a retrieval
problem's clothes, and the repository spent five phases on the retrieval half.

### Three prior measurements that constrain the reading

1. **The retrieval ceiling predicted this shape.** 22.7% of gold references sit
   outside the 50-candidate pool — 0% on single-hop, 35% on aggregation. The
   prediction was parity at the easy end and collapse at the hard end. That is
   exactly what happened. What the hybrid failed to do was repair the hard end.
2. **The hybrid discards two-thirds of its own retrieval.** `first-50` retains
   **20 of the 61** gold chunks the graph path reaches (ADR-0014). It enters the
   comparison pre-handicapped, and `uncapped` reaching all 61 says the statements
   were found and then dropped.
3. **Nothing is near any ceiling.** The best cell in the table is 16/20.

## Judge agreement

**17 of 20 (85%)** against a stratified holdout hand-graded by the project owner,
blind: `eval/grade_holdout.py` withholds `verdict`, `judge_reason`, `judge_defect`
and `judge_capped_from`, and `test_grade_holdout_never_prints_a_verdict` enforces
the omission. *Which* 20 rows was fixed by `judge.holdout()` from the eval set
alone, before any answer existed.

| Row | Hand | Judge | Direction |
|---|---|---|---|
| `3h-011` | wrong | partially_correct | judge lenient |
| `hn-006` | partially_correct | wrong | judge harsh |
| `th-016` | correct | partially_correct | judge harsh |

**The disagreements do not share a direction**, and that is the finding that
matters most here. Before the hand pass there was a live hypothesis that the ~45%
`partially_correct` rate was an artefact of grading rules written too strictly —
in which case the whole table would have been measuring rule-writing rather than
systems. Symmetric errors do not support that hypothesis, so the negative result
stands on its own rather than resting on the grader.

The one lean worth recording: on `3h-011` the judge accepted a **refusal to an
answerable in-corpus question** as `partially_correct`, where the hand grade calls
it `wrong` — it is a retrieval failure, not a partial answer. The judge agreed
with the hand grade on two other refusal-on-answerable rows (`th-006`, `th-011`),
so this is one row rather than a pattern, but it is the direction to watch.

### The hand pass found a defect in the harness, not just in the answers

`judge --agreement` originally filtered on `system == "hybrid"` and **not** on
`mode`. A row in the 30-row live subsample therefore carries two hybrid verdicts,
and the later live row silently overwrote the replay one — while the hand labels
describe the *replayed* answers, because that is what `grade_holdout` prints.

`oos-001` was the only holdout row in the live subsample. Its replay answer was
graded `wrong` (it opens with a refusal and then produces a substantive answer
with 11 citations); its live answer was graded `correct_refusal`. The comparison
picked up the live verdict and reported a disagreement in which the judge appeared
to have excused a fabricated answer on an out-of-scope question — the single
safety behaviour this eval set cares most about. The judge had done no such thing.

True agreement is 17/20 rather than 16/20. The fix is one clause plus
`test_agreement_compares_replay_verdicts_only`. **A hand-verification sample found
a bug in the verification harness itself**, which is the whole argument for having
one.

## Open

- **`partially_correct` is where the signal is, and no metric reports it.** The
  pass rate discards ~45% of every arm identically. A partial-credit score
  (correct = 1, partial = 0.5) would discriminate far better — but it was not
  pre-registered, and adopting it *after* seeing the pre-registered metric produce
  a null is precisely the move ADR-0015 exists to prevent. Pre-register it for a
  future run; do not swap it into this one.
- **The budget confound is unresolved and is the highest-value follow-up.** The
  hybrid ran at `first-50`, retaining 20 of 61 reachable gold chunks. Re-running
  that one arm at `uncapped` would say whether the hybrid loses because graph
  statements do not help, or because the adopted budget threw most of them away
  before the model saw them. One arm, about $1.
- **Three-hop is 0–2 of 14 for everything.** Either those 15 questions are badly
  posed or the whole stack fails at three hops. The eval set and the systems'
  prompts share an author, so an independent read of those rows is worth more than
  another benchmark run.
- **The refusal strata got *worse* under the hybrid** (11/20 → 8/20 against
  vector-only). More documents in the prompt appears to make declining harder, and
  `oos-001`'s replay answer — refuse, then answer anyway with 11 citations — is
  the shape of it.
- **Aggregation denominators are the smallest** (7–9 of 10 scored) because
  aggregation rows truncate most often. The stratum with the largest gold sets is
  the one `MAX_TOKENS` bites hardest, and those rows are dropped from exactly the
  arms that retrieved the most — a bias against the systems this benchmark was
  built to favour.
- **p95 rests on n=30** and is labelled with its denominator everywhere it appears.
