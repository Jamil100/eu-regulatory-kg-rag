# ADR 0012: Route with deterministic rules; Command R7B did not clear the bar

- **Status:** Accepted
- **Date:** 2026-08-03
- **Deciders:** Jamil (project owner)
- **Context phase:** Phase 3 — Step 3, router

## Context

The roadmap names Command R7B for this stage and makes the choice part of the
argument: *"Router only emits `graph` / `vector` / `both`. R7B is dramatically
cheaper (~$0.0375/$0.15 per 1M tokens) and fast — the right tool for a
high-volume classification call. Using the small model here is itself a
cost-engineering signal."*

That is a prediction, and this project has twice found the deterministic stage
was the one that worked. ADR-0009 is an entire ADR about an embedding stage that
scored *below* its own control: `dpo` vs `data protection officer` at **0.42**,
under legitimately-distinct pairs at **0.75**. Step 2 then built the entity
linker with no embedding stage at all and got 23-of-23 link rate for **$0.00**.

So both routers were built and both were measured on the same 23 hand-labelled
rows.

### What was fixed before the measurement ran

1. **Accuracy** over the gold rows.
2. **A gap of ≤ 1 row is a tie**, and a tie goes to the deterministic baseline —
   it costs $0.00 and adds no network hop.
3. **A hard gate.** Either router fails outright if it routes a
   `graph_traversable: false` row to `graph`. `xr-003` and `xr-004` compare AIA
   Art. 99 to GDPR Art. 83, which never cross-cite; two independent readings
   already reached that conclusion (the eval author read the law, and Step 2's
   linker reached nothing those rows' gold chunks assert).
4. **Two constant arms are reported.** Necessity labelling was expected to make
   one class dominant, so `always-vector` (the majority class, 13 of 23) and
   `always-both` (a defensible production router that simply runs both paths) are
   in the table. A router that cannot beat a constant has not earned a place in
   the request path.

Pre-registering 2 and 3 matters because both ended up load-bearing, and deciding
either after seeing the numbers would have been indistinguishable from choosing
the winner first.

### Gold labels

`route` was added by hand to all 23 rows (`eval/eval-questions.jsonl`), seeded
from `graph_traversable` / `ontology_edges` / `hops` and then read against the
source text — derive-then-verify, never derive-and-trust. 21 of 23 agree with the
seed; the two that do not (`ag-001`, `ag-003`) carry a `route_reason`, and
`tests/test_eval_questions.py` refuses a silent override. Distribution:
**13 `vector`, 9 `both`, 1 `graph`**.

## Decision

**Adopt the deterministic rules. `src/query/router.py:ADOPTED = "rules"`.**

Measured over 22 rows (`3h-002` sits in the `expected_fail` bucket, as the
benchmark already reports it):

| arm | correct | accuracy | hard gate | cost / query | latency p50 | p95 |
|---|---|---|---|---|---|---|
| **rules** | **21 / 22** | **95%** | ok | **$0.00** | **3.5 ms** | 12.2 ms |
| r7b | 10 / 22 | 45% | **FAIL** | $0.0000338 | 275 ms | 9,185 ms |
| always-vector | 13 / 22 | 59% | ok | $0.00 | — | — |
| always-both | 8 / 22 | 36% | ok | $0.00 | — | — |

This is not a tie, so rule 2 never came into play. R7B failed the gate on
`xr-004` and would have been disqualified even had it won on accuracy.

The full sweep cost **$0.000777** for 23 questions. Cost was never the issue and
the roadmap's cost argument for R7B is sound — it is simply an argument about the
wrong axis, because the classification did not work.

### The finding: R7B never emits the third class

R7B returned `both` for **0 of 23** questions. Its output was `vector` 15× and
`graph` 8×, with zero unparseable answers. The errors are not spread across a
confusion matrix — one entire class is missing, and since 9 of 22 scored rows are
gold `both`, that alone caps R7B at 13/22 before a single judgement is made.

```
r7b: confusion (rows = gold, cols = predicted)
              both   graph  vector
both             0       3       5
graph            0       1       0
vector           0       4       9
```

Two of the six few-shot examples demonstrate `both`, which
`tests/test_router.py::test_all_three_classes_are_demonstrated_to_the_model`
asserts, so this is a fact about the model on this task rather than a prompt that
forgot to mention the class.

**The prompt was then rewritten to attack exactly that, and it did not move.** The
class collapse is visible from the *output distribution alone* — no gold label is
needed to see that a three-way classifier used two classes — so repairing it is a
fair fix rather than fitting to the answers. The second prompt replaced the
descriptive framing with an ordered decision procedure that tests `both` **first**
and says so:

```
1. Does the question ask about two connected things -- a fact and then something
   related to it ("..., and who ...", "..., and what ..."), or one regulation and
   then the other? Answer: both. This is the most common case; do not skip it.
2. Otherwise, is the answer a SET or a CHAIN ...  Answer: graph.
3. Otherwise ... Answer: vector.
```

Result: **`both` still 0 of 23**, accuracy unchanged at 10 of 23, the `xr-004`
gate failure unchanged, and one answer became unparseable (16 `vector`, 6 `graph`,
1 neither). The rejected prompt is recorded here rather than in the module,
because it is a result about an alternative, not code anyone should run.

### The comparison is asymmetric, in the rules' favour

The rules were authored with all 23 gold labels visible. Their 95% is an
**in-sample number and an upper bound**, and it should be read as "rules can
express this labelling", not "rules will score 95% on the next 77 questions".
R7B saw none of the labels; its few-shot examples are hand-written questions
about the same two regulations that appear nowhere in the eval set, asserted
mechanically by `test_few_shot_examples_are_not_in_the_eval_set`.

Two things stop that asymmetry from swallowing the result:

- The gate failure is not an accuracy question. `xr-004` → `graph` disqualifies
  R7B on a rule fixed in advance.
- The missing class is not an accuracy question either. A classifier that never
  emits one of three labels is broken in a way no amount of out-of-sample
  generosity repairs, and it lost to a constant.

Honest form of the claim: **rules beat R7B decisively; the size of the win is
inflated by in-sample authoring, and the eval set at 100 rows is what will say by
how much.**

### R1 is inert, and is reported as inert

The phase plan called *"a question that links to zero nodes has no graph path
available"* a strong rule. Step 2 measured the link rate at **23 of 23**, so R1
fires on **no row in this eval set**. The rule that actually carries the refusal
and penalty rows is **R2** — no linked node has a type any template declares as a
parameter (`ag-003` reaches only `GDPR` and `infringement`). Rules fired: R5 9×,
R4 8×, R2 5×, R3 1×, **R1 0×**.

R1 is kept as a genuine guard on the request path, where a question about
something the corpus has never heard of will link to nothing, and
`test_r1_is_inert_on_this_eval_set` asserts the inertness so that a linker
regression turns it load-bearing loudly.

## Consequences

- `src/query/router.py` calls no API on the request path. The router contributes
  **$0.00 and ~3.5 ms** to `/ask`, so Step 7's `cost_usd` is entirely embed,
  rerank, and generate. The roadmap's "R7B-vs-A routing economics from your own
  logs" story survives, with a different ending: the cheapest model was still too
  expensive relative to free.
- **The decision log is built and append-only** (`src/query/decision_log.py`),
  closing the shape of `failure-notes.md` §3 for this file: the roadmap says
  Phase 5 needs these decisions and cannot reconstruct them, and the repo's only
  other JSONL writer rebuilds its file on every run.
- `Route` moved from `router.py` to `src/schemas.py`, where `AskResponse` had
  been spelling the same three values a second time with nothing pinning the two
  lists together.
- **This is reversible on evidence, not on preference.** `ADOPTED` is one
  constant. If the eval set at 100 rows shows the rules were fitted — the honest
  risk — the artifact, the sweep, and the four-arm table are all still here to
  re-run.
- Adopting rules means the router has **no confidence score**, so the roadmap's
  *"below a confidence heuristic, run both paths"* is not available. `both` is now
  reached only by R4. If that proves too blunt, a confidence-like signal exists
  for free in the linker (count and type of anchors) and was not needed yet.

---

## Amendment, 2026-08-15 — re-measured at 100 rows; the risk this ADR named came true

**Status of the decision: unchanged. Status of the headline number: withdrawn.**

This ADR closed by naming its own failure mode:

> **This is reversible on evidence, not on preference.** `ADOPTED` is one
> constant. If the eval set at 100 rows shows the rules were fitted — the honest
> risk — the artifact, the sweep, and the four-arm table are all still here to
> re-run.

The eval set reached 100 rows on 2026-08-15 and the sweep was re-run. It did show
the rules were fitted.

| Arm | 23 rows | **100 rows** |
|---|---|---|
| rules | 21/22 — **95%** | 70/99 — **71%** |
| R7B | 10/22 — 45% | 44/99 — 44% |
| always-vector | 13/22 — 59% | 48/99 — 48% |
| always-both | — | 47/99 — 47% |

**The adoption stands.** Rules still beat every constant and still beat R7B, which
is the criterion this ADR actually decided on. Nothing about the choice of
deterministic rules over the model is disturbed — if anything R7B looks worse, and
the finding it turned on is unchanged: **R7B emitted `both` 0 times in 99 rows**,
under the same two prompts, so one whole class is still missing rather than its
errors being spread over a confusion matrix.

**The 95% is withdrawn as a description of the router.** It was measured on the 23
questions the rules were written while looking at, and it does not survive contact
with 77 unseen ones.

### The failure is systematic, and this ADR already named the mechanism

> `both` is now reached only by R4. If that proves too blunt, a confidence-like
> signal exists for free in the linker (count and type of anchors) and was not
> needed yet.

It proved too blunt. `R4-second-ask` is one regex —
`,? and (who|what|how|where|does|is|are|against)` — and **24 rows whose gold is
`both` are routed `vector`** because they express the second hop in a shape it
does not match:

| Row | Question shape | Why R4 misses |
|---|---|---|
| `th-012` | "…Which GDPR fine tier applies?" | the second hop is semantic, not syntactic; there is no conjoined second clause at all |
| `th-019` | "when must a controller tell X and when must it tell Y" | `and when` is not in the alternation |
| `3h-009` | "What assessment must it run, what must it do if…" | comma-separated asks, no `and` |
| `xr-005`, `xr-010` | single-clause cross-regulation questions | same as `th-012` |

`R2-no-anchor` accounts for the rest, including both `graph` misses (`ag-005`,
`ag-006`), where the linker reaches nothing a template accepts as an anchor.

### What was done about it, and what deliberately was not

**Not done: retuning the regex.** Widening it against misses that have already
been seen is how the 95% was produced in the first place. Doing it again, on the
set the benchmark is about to be computed from, would buy a better number and no
better router. A future step that wants to fix this should hold out a split first.

**Done: the benchmark stopped confounding two things.** `eval/run_benchmark.py`
grew a fourth arm, `hybrid-oracle`, which replays the hand-verified gold route.
The pair is a decomposition rather than a flattering ceiling:

- `hybrid-oracle` — what the hybrid can do when it is asked correctly
- `hybrid` — what the deployed system does today, and the honest per-query number
- the gap — **the router's cost, reported as a number instead of as a caveat**

Without it, a weak `hybrid` cell means either "the graph path could not answer" or
"the router never asked it to", and the table cannot say which. See
ADR-0015 §Decision 1 and `test_the_router_cost_is_computed_over_rows_both_hybrid_arms_scored`.

### One rule changed status

**R1 (`links to zero nodes → vector`) is no longer inert.** It fired on 0 of 23
rows and now fires on exactly one of 100: `oos-005`, "What labelling does China's
deep synthesis regulation require?" — a question about a regulation the corpus
does not contain, where reaching no node is the correct outcome rather than a
linker regression. R1 sends it to `vector`, its gold route, so R1 is now a rule
that earns an answer rather than a guard that never runs.

This is not "out-of-scope rows link nothing": the other four out-of-scope rows do
link, because they name concepts the EU corpus also uses. Refusal still cannot be
delegated to retrieval coming back empty. `test_r1_is_load_bearing_on_exactly_one_row`
pins both halves.
