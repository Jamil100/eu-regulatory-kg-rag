# Query path metrics

Status: **Entity linker, router and vector path built and measured. 23 of 23 eval
questions link to at least one graph node; 52% of links land in a gold chunk's
entity set (64% excluding instrument nodes). Routing is deterministic: 21 of 22
rows, $0.00, 3.5 ms — Command R7B scored 10 of 22, below the majority-class
constant, and never emitted `both` at all. Rerank 3.5 over the top-50 lifts micro
recall from 23/51 to 27/51 at k=5 and 28/51 to 31/51 at k=10 — a real aggregate
gain, and not one per-stratum move clears this eval set's own resolution. The
graph path now runs end to end: deterministic template selection reaches **24 of
32 gold chunks — the oracle exactly** — against R7B's 14, at $0.00 and 5.7 ms,
and `ag-001` comes back **11 of 11** where top-10 similarity cannot return 11
rows at all.**
Phase 3 Steps 2–5 complete. **Step 6 is measured in
`docs/metrics/answer-path.md`** — split out because this file is already 650+
lines and because generation, citation validation and the statement budget are a
different subject from retrieval. Step 7's per-route latency and cost table
appends there too.

Fifth companion to `extraction-cost-and-findings.md` (what came out of the
model), `graph-load.md` (what came out of the loader), `eval-set.md` (the
instrument) and `vector-index.md` (the vector half of the store). This one
measures what *reads* the store: the path from a question to an answer.

Regenerate with:

```bash
python -m src.query.entity_linker --eval             # every number below
python -m src.query.entity_linker --eval --from-db   # gold cross-check vs pgvector
python -m src.query.entity_linker --question "..."   # link one question
pytest tests/test_entity_linker.py

python -m src.query.router --eval                    # the router table, from the artifact
python -m src.query.router --eval --refresh          # re-run R7B live (~$0.0008)
python -m src.query.router --question "..."          # both routers, one question
pytest tests/test_router.py tests/test_eval_questions.py

python -m src.query.reranker --eval                  # the k-matrix, from the artifact
python -m src.query.reranker --eval --refresh        # re-run live (~$0.05)
python -m src.query.retriever --question "..."       # top-50, no rerank
python -m src.query.reranker  --question "..."       # retrieve then rerank
pytest tests/test_retriever.py tests/test_reranker.py

python -m src.query.template_selector --eval           # the graph table, from the artifact
python -m src.query.template_selector --rebuild        # re-execute committed plans; no key, no spend
python -m src.query.template_selector --eval --refresh # re-run both arms live (~$0.001)
python -m src.query.template_selector --question "..." # both arms, one question
python -m src.query.graph_path --question "..."        # select, execute, render
python -m src.query.graph_query --baseline             # the six row-count anchors
pytest tests/test_template_selector.py tests/test_path_to_prose.py
```

---

## Shape

| | |
|---|---|
| Index surfaces | **3,366** canonical + **3,322** normalised alias |
| Alias surfaces naming >1 node | **372** |
| Surfaces that are one node's name *and* another's alias | **59** |
| Plural merges | **124** (the resolver's own 18, widened — see below) |
| Questions linking to ≥1 node | **23 / 23** |
| Rows scoreable against gold | **21** (2 refusal rows carry no gold chunks) |
| Links emitted over 21 scoreable rows | **75** (3.6 per question) |
| Build time / link time | **0.17 s** once, **5.5 ms** per question |
| API calls, cost | **0**, **$0.00** |
| Distinct names linked across the 23 questions | **38**, of which **38** exist as live `:Entity` nodes |
| Chunks where the resolver and pgvector `entity_ids` disagree | **0** of 1,107 |

## The output is consumable, checked against the live graph

The contract is not "returns strings" but "returns strings a template accepts", so
it is verified that way rather than by inspection. All **38** distinct names the
23 questions link to exist as `:Entity` nodes in Neo4j, and every linked
`ActorRole` fed straight into Step 1's `obligations_for_role` returns rows with
provenance:

| Linked role | Rows | Provenance ids |
|---|---|---|
| `provider` | 210 | 259 |
| `deployer` | **60** | 71 |
| `sme` | 10 | 10 |
| `start up` | 5 | 5 |
| `financial institution` | 3 | 4 |
| `worker` | 1 | 1 |

`deployer` at 60 is the `graph-load.md` §Template baseline anchor, reached from
`ag-001`'s wording ("obligations the AI Act places on **deployers**") rather than
hand-typed — which is the whole point of the step. `graph_query --baseline` still
reports all six anchors unchanged.

The gold set is taken from the resolver by default so the measurement runs with
no containers; `--from-db` reads pgvector's `entity_ids` instead and the two
agree on **all 1,107** chunks, which is what licenses the shortcut
(`schema.sql:50-53` promises exactly this and nothing had checked it from the
query side).

## Linker quality, per stratum

| Stratum | link rate | hit rate | precision | recall* |
|---|---|---|---|---|
| two-hop | 4/4 **100%** | 4/4 **100%** | 8/11 **73%** | 12% |
| three-hop | 2/2 **100%** | 2/2 **100%** | 8/12 **67%** | 19% |
| single-hop | 6/6 **100%** | 6/6 **100%** | 13/24 **54%** | 21% |
| aggregation | 3/3 **100%** | 2/3 **67%** | 4/8 **50%** | 3% |
| cross-regulation | 4/4 **100%** | 2/4 **50%** | 6/15 **40%** | 7% |
| hard-negative | 2/2 **100%** | 0/2 **0%** | 0/5 **0%** | 0% |
| **all** | **21/21 100%** | **16/21 76%** | **39/75 52%** | **10%** |

- **link rate** — rows reaching ≥1 node at all. A question that links to nothing
  has no graph route available, whatever Step 3's router decides, so this is the
  number that gates the whole path. It is 100%.
- **hit rate** — rows where ≥1 linked node is asserted by a gold chunk.
- **precision** — linked nodes that a gold chunk asserts, over all linked nodes.
- **recall\*** — the same numerator over *every* entity the gold chunks assert.

### The recall denominator is a superset, and that is why precision is the headline

The plan proposed gold = the gold chunks' `entity_ids`, for a good reason: it
costs no new hand-labelling, and it is a genuine cross-check rather than a
restatement, because `entity_ids` came from the resolver while the gold chunks
came from reading the law. But measured before use, that set averages **18.9
nodes per row** and reaches **75** for `ag-001`, while a question names three or
four things. A linker emitting 3.6 nodes per question **cannot** reach 100% of a
19-node denominator, so the 10% recall column is a lower bound fixed by
arithmetic, not a finding about the linker. It is reported because deleting an
inconvenient column is worse than labelling it, and labelled because this file's
own siblings record two cases of a number that looked like a measurement and was
not.

Precision has no such defect, and it says something: **14 of the 36 misses are
`Regulation` nodes** — `AI Act` (9×) and `GDPR` (5×). Almost every question names
its instrument ("under the AI Act…") and almost no chunk asserts the instrument
as an entity, so the gold set structurally cannot credit a link that is
completely correct. Excluding them, precision is **64%**. Both numbers are kept:
52% is what the stated metric says, 64% is what the linker deserves, and the gap
is a property of the denominator.

### Every zero is a penalty question, and the reason is vocabulary

The five rows that hit nothing are `hn-001`, `hn-002`, `ag-003`, `xr-003`,
`xr-004`. All five ask about fines. Their gold entity sets explain it:

```
hn-001  gold: aia art. 5 | aia art. 99(3)
              administrative fine up to eur 35 000 000 or 7 % of total worldwide annual turnover
ag-003  gold: gdpr art. 5 | gdpr art. 12 | gdpr art. 22 | gdpr art. 44 | ... (22 nodes)
              administrative fine up to eur 20 000 000 or 4 % of total worldwide annual turnover
```

A penalty chunk asserts **article-citation nodes** and one **very long penalty
name**. A question says "the highest fine tier" and "categories of infringement";
it never spells out `administrative fine up to eur 20 000 000 or 4 % of total
worldwide annual turnover`, and it names articles only when it happens to quote
one. So for these rows the two vocabularies barely intersect — the linker links
what the question *says*, the gold set holds what the chunk *asserts*, and on
penalty rows those are disjoint by nature. `ag-003`'s links (`GDPR`,
`infringement`) are the correct reading of the question and score 0.

**This is the metric's limit, not the linker's,** and it is the sharpest argument
for the hand-labelled entity set the plan deliberately deferred.

Separately, and worth more than the percentage: the two cross-regulation misses
are **`xr-003` and `xr-004`** — exactly the two rows the eval set already
hand-labels `graph_traversable: false` ("endpoints never cross-cite; no
article-level bridge is derivable"). Two independent routes to one conclusion —
the eval author read the law and said no path exists; the linker, working from
the graph, reaches nothing those chunks assert. Step 3's router should send both
to `vector`.

## What was reused, and the one rule that was widened

`normalize()` and `_plural_map()` are imported from `src/ingest/entity_resolution.py`
rather than reimplemented, and `resolve_corpus()` supplies the three
corpus-dependent parts a question cannot: node types, alias lists, and the fold.

The fold is the one thing changed, with the change measured before adopting.
`resolve_corpus()["plural_merges"]` folds only surfaces that appeared as an
extracted `canonical_name` — **18** entries — and aliases never went through it.
Questions use plurals freely (`ag-001` asks about "deployers"), so the same
`_plural_map` rule is re-run over canonical names *and* alias surfaces: **124**
merges, **102** of them a plural resolving to a real node. Before adopting:

- **0** of the extra merges fold a surface that is itself a node.
- The breakages the rule exists to prevent stay unmerged — `premises`,
  `analysis`, `business`, `bias`, `practices` — because their singulars are not
  attested. The rule is still data-driven, only the pool of attested surfaces is
  wider.

Both are enforced by tests, not asserted here.

**No embedding stage.** The Phase 0 stub promised "alias lookup + Embed v4
similarity". ADR-0009 measured the similarity half and it was the stage that did
not work: `dpo` vs `data protection officer` scored **0.42**, *below* pairs that
are legitimately distinct (`supervisory authority` vs `lead supervisory
authority`, **0.75**), so no threshold separates the classes. The bar it would
have to clear is now concrete: beat **100%** link rate and **52%** precision at
**$0.00** and **5.5 ms**, on a path where a wrong link is a silent zero-row
template call. The stub's docstring was corrected to describe what exists.

## Defects found on first contact with real questions

Both returned plausible results, which is why both are regression tests rather
than comments.

| Defect | Symptom | Fix |
|---|---|---|
| `_trim` peels `" .,;:"` but not `?` — no corpus name ends in a question mark | `"...require a notified body?"` missed `notified body`, fell back to the token `notified`, and linked the obligation **`notify use of real time remote biometric identification system`** | Strip `?!` at the linker boundary. **Not** in `_trim`, which would move corpus node keys |
| `normalize()` deletes apostrophes, turning a possessive into a plural no fold covers | `"the GDPR's highest fine tier"` normalised to `gdprs`; `ag-003` linked **no instrument at all** | Try the possessive-stripped form *after* the plain one, single-token spans only |

The second fix had a defect of its own, found the same way. Applied to
multi-token spans it let `"the controller's"` — stripped to the alias
`the controller` — shadow `"controller's representative"` one token to the right,
because the sweep is leftmost-longest. Restricting the fallback to the token that
carries the apostrophe costs nothing this eval set exercises and is recorded in
§Open as the remaining gap.

## Router: deterministic rules vs Command R7B

Gold `route` labels were added by hand to all 23 rows and are what everything
below is scored against. They were seeded mechanically (`graph_traversable`,
`ontology_edges`, `hops`) and then read against the source text; 21 of 23 agree
with the seed and the two that do not carry a `route_reason`. Distribution:
**13 `vector`, 9 `both`, 1 `graph`**.

Scored over **22** rows — `3h-002` is in the `expected_fail` bucket, where the
benchmark already reports it, rather than counted as a router failure for an
extraction gap.

| arm | correct | accuracy | hard gate | cost/query | p50 | p95 |
|---|---|---|---|---|---|---|
| **rules** | **21 / 22** | **95%** | ok | **$0.00** | **3.5 ms** | 12.2 ms |
| r7b | 10 / 22 | 45% | **FAIL** | $0.0000338 | 275 ms | 9,185 ms |
| always-vector | 13 / 22 | 59% | ok | $0.00 | — | — |
| always-both | 8 / 22 | 36% | ok | $0.00 | — | — |

The two constant arms are the point of the table, not padding. Necessity
labelling was adopted knowing it might make one class dominant, and the agreed
mitigation was to report what a constant scores. **R7B lost to
`always-vector`** — 45% against 59% — so it is not merely worse than the rules,
it is worse than not classifying at all.

The **hard gate** was fixed before the run: never route a `graph_traversable:
false` row (`xr-003`, `xr-004`) to `graph`. R7B sends `xr-004` to `graph`, which
disqualifies it independently of accuracy.

### R7B never emitted the third class

```
r7b: confusion (rows = gold, cols = predicted)      rules: (same layout)
              both   graph  vector                            both   graph  vector
both             0       3       5                  both         7       0       1
graph            0       1       0                  graph        0       1       0
vector           0       4       9                  vector       0       0      13
```

**0 of 23** outputs were `both`; 15 were `vector`, 8 `graph`, 0 unparseable. Since
9 of the 22 scored rows are gold `both`, the missing class caps R7B at 13/22
before any individual judgement is made. Two of six few-shot examples demonstrate
`both`, which a test asserts, so this is a property of the model on this task and
not a prompt that forgot the class.

A second system prompt was written specifically to attack it — an ordered
decision procedure testing `both` first and telling the model it is the most
common case. **`both` stayed at 0 of 23**, accuracy stayed at 10 of 23, the gate
failure stayed, and one answer became unparseable. The rejected prompt is in
ADR-0012. Fixing a class the model never emits is diagnosable from the output
distribution alone, which is what made that a fair repair attempt rather than
fitting to the gold labels.

### Per stratum

Rules against R7B against the majority-class constant. Two strata are 2 rows and
one is 1, so read the small ones as direction, not rate.

| Stratum | n | rules | r7b | always-vector |
|---|---|---|---|---|
| single-hop | 6 | **6/6** | 3/6 | 6/6 |
| two-hop | 4 | **3/4** | 0/4 | 0/4 |
| three-hop | 2 | **2/2** | 0/2 | 0/2 |
| cross-regulation | 4 | **4/4** | 1/4 | 2/4 |
| aggregation | 3 | **3/3** | 2/3 | 1/3 |
| hard-negative | 2 | 2/2 | 2/2 | 2/2 |
| out-of-scope | 1 | 1/1 | 1/1 | 1/1 |
| unanswerable | 1 | 1/1 | 1/1 | 1/1 |

R7B scores **0 of 6** on two-hop and three-hop combined — precisely the strata
whose gold label is `both`, and precisely the strata this phase exists to move
(vector-only recall is 30% and 67% there). The refusal strata are where every arm
agrees, because a constant `vector` is correct for all four of those rows.

### Which rules carry the load

| Rule | Fires | Route |
|---|---|---|
| R5 default | 9 | `vector` |
| R4 conjoined second question | 8 | `both` |
| R2 no linked node is a template anchor | 5 | `vector` |
| R3 enumerative + `ActorRole` | 1 | `graph` |
| **R1 zero linked nodes** | **0** | `vector` |

**R1 is inert and is reported as inert.** The phase plan called "a question that
links to zero nodes has no graph path available" a strong rule; Step 2 measured
the link rate at 23 of 23, so it fires on nothing here. The rule that actually
does that job is **R2** — a question can link to five real nodes and still have
nothing to traverse from, because `Regulation`, `DefinedTerm`, `Authority` and
`Penalty` are not parameters any template declares. `ag-003` reaches only `GDPR`
and `infringement`. R1 is kept as a real guard on the request path and a test
asserts its inertness, so a linker regression makes it load-bearing loudly rather
than silently.

The single rules miss is **`th-004`** ("A deployer of a high-risk AI system fails
to meet its Article 26 obligations. Which penalty applies?"), gold `both`, routed
`vector`. It names an `Article` and an `ActorRole` and needs the
`PENALIZED_UNDER` chain, but has no conjoined second clause for R4 to see. The
obvious repair — treat `Article` + one other anchor as a traversal — also moves
`oos-002` the wrong way, so it was left alone rather than tuned to one row.

### The comparison is asymmetric and the number is in-sample

The rules were authored with all 23 gold labels visible. **95% is an upper
bound**, and it says "these rules can express this labelling", not "these rules
will score 95% on the next 77 questions". R7B saw none of the labels; its
examples are hand-written questions that appear nowhere in the eval set, asserted
mechanically rather than promised in a comment. What survives the asymmetry is
the gate failure and the missing class, neither of which is an accuracy question.

## Vector path: retrieval and rerank

21 labeled queries, 51 gold references, **512 dims** (ADR-0004), exact search,
top-50 candidates, all 50 reranked. Source: `eval/rerank-eval.jsonl`, which
stores the full retrieved and reranked orderings per question — every number
below is recomputed from that file by `scoreboard()` with no database, no API key
and no spend.

### The denominator is capped, and reporting it wrongly would have flattered rerank

Micro recall cannot reach 100% at small k, because a question with 11 gold chunks
can contribute at most k of them. The ceiling is `Σ min(gold_i, k)`:

| k | ceiling | max micro recall |
|---|---|---|
| 5 | 45/51 | **88.2%** |
| 10 | 50/51 | **98.0%** |
| 50 | 51/51 | 100% |

**The phase plan asked for "recall@5-after-rerank against recall@10-before".**
That comparison charges a *perfect* reranker 9.8 percentage points before it
starts, and the entire k=5-vs-k=10 gap is one row: `ag-001` declares 11 gold
chunks and every other question has at most 4. So the tables here report **pre@k
against post@k at the same k**, with the ceiling printed beside every figure.

Only `aggregation` is capped below 100% at k=5 (9/15). Every other stratum —
including **two-hop, the number this step exists to move** — is uncapped at both
k, so those deltas need no adjustment at all. Restating ADR-0004's aggregation
figure against what is achievable rather than against 15: **7/14 = 50%**, not
7/15 = 47%.

### The k-matrix

| k | pre-rerank | post-rerank | delta | ceiling | top-50 oracle | hit rate pre → post |
|---|---|---|---|---|---|---|
| 5 | 23/51 — 45.1% | **27/51 — 52.9%** | **+4** | 45/51 | 37/51 | 76.2% → **85.7%** |
| 10 | 28/51 — 54.9% | **31/51 — 60.8%** | **+3** | 50/51 | 41/51 | 85.7% → 85.7% |
| 50 | 41/51 — 80.4% | 41/51 — 80.4% | 0 | 51/51 | — | 95.2% → 95.2% |

The k=50 row is 0 by construction — reranking all 50 candidates is a permutation,
so it cannot change what the set contains. It is in the table as a check on the
other two rows, and a test asserts it: if it ever moves, the sweep stopped
reranking the full pool and the arms are measuring different candidate sets.

**Pre-registered before the first rerank ran:** ADR-0004 declared 2 gold chunks
out of 51 to be inside this eval set's resolution and refused to decide
1536-vs-512 on a 1-chunk difference. The same threshold binds here. **+4 at k=5
and +3 at k=10 clear it. Nothing else in this section does.**

### Per stratum, where nothing clears the threshold

| Stratum | n | gold | pre@5 | post@5 | Δ | pre@10 | post@10 | Δ | cap@10 | oracle@10 |
|---|---|---|---|---|---|---|---|---|---|---|
| two-hop | 4 | 10 | 3/10 | 4/10 | +1 | 3/10 | 4/10 | +1 | 10 | 7 |
| aggregation | 3 | 15 | 5/15 | 5/15 | 0 | 7/15 | 8/15 | +1 | 14 | 12 |
| cross-regulation | 4 | 10 | 5/10 | 7/10 | +2 | 6/10 | 7/10 | +1 | 10 | 7 |
| three-hop | 2 | 6 | 3/6 | 3/6 | 0 | 4/6 | 4/6 | 0 | 6 | 5 |
| single-hop | 6 | 8 | 6/8 | 6/8 | 0 | 6/8 | 6/8 | 0 | 8 | 8 |
| hard-negative | 2 | 2 | 1/2 | 2/2 | +1 | 2/2 | 2/2 | 0 | 2 | 2 |

**`vector-index.md` §Open named Rerank 3.5 "the obvious lever on the 30% two-hop
figure". It is not.** Two-hop moves 3/10 → 4/10: one chunk, half the
pre-registered resolution. The aggregate gain is real and the per-stratum story
is not supported at this sample size — every cell in the Δ columns is 0, +1 or
+2, and a test asserts that so the day one of them clears ±2 it has to be written
down here rather than quietly absorbed.

The largest single move, cross-regulation +2 at k=5, is also the stratum where
512 loses its one chunk to 1536. Reading it as a rerank effect and the dimension
loss as a separate fact would be double-counting the same two questions.

### The candidate pool is not the binding constraint — ranking is

The top-50 holds **41 of 51** gold references. So reordering alone could reach
41/51 at k=10; it reaches 31/51. **Rerank captured 3 of the 13 chunks available
to it and left 10 on the table.** The `oracle` column above is the per-query form
`Σ min(|gold_i ∩ top50_i|, k)`, not the looser `min(recall@50, cap@k)`, so it is
a ceiling that is actually reachable.

Two-hop is the sharpest case: 7 of its 10 gold chunks are in the pool and 4 come
back at k=10. Whatever is wrong with two-hop retrieval, **it is not that the
right paragraphs were never retrieved.** That points at ranking and at
paragraph-level chunking (ADR-0003, `vector-index.md` §Open) rather than at the
embedding.

### Latency, in three components

`vector-index.md` quotes **6.68 ms p50**, which is SQL alone with the query
vector already in hand. A request pays an embedding round trip to get it, and the
vector route pays a rerank round trip after. Reported separately so Step 7's
end-to-end number is not read as a 50× regression against something that never
measured the same thing.

| component | p50 | note |
|---|---|---|
| embed | 30 ms | amortised over one batched call for 23 questions; a single `/ask` pays **~330 ms** |
| search | 9.3 ms | SQL only, k=50 — consistent with ADR-0004's 6.68 ms at k=10 |
| rerank | **286 ms** | 50 documents, one search unit |

**The p95 is not reported, because it would be a fact about the API key.** Three
of 23 rerank calls took far longer than the rest — `hn-001` 83.5 s, `sh-006`
82.4 s, `th-003` 3.9 s. The measurement ran on a Cohere trial key (10
calls/minute). The other 20 calls returned in 230–340 ms. `scoreboard()` lists
the stalls by name and the report prints "quote the p50, not the p95"; a
percentile over 23 calls with that tail describes the account, not Rerank 3.5.

> **Correction (Step 6, 2026-08-05).** This paragraph previously read *"all with
> `attempts=1`, so this was not retry backoff"* and concluded that the trial tier
> holds a request open rather than returning 429. **The evidence for that does
> not exist.** `attempts` was read via `_call.retry.statistics`, which is
> permanently `{}` in tenacity ≥ 8.2.3 — the `@retry` wrapper runs `copy =
> self.copy()` per invocation and assigns *the copy's* statistics to
> `wrapped_f.statistics`, while `wrapped_f.retry` stays a controller that never
> executes. So `.get("attempt_number", 1)` returned the default on every call at
> every site, and `attempts=1` was a tautology rather than an observation. Two
> 82-second stalls are also squarely inside what `wait_exponential_jitter(
> initial=2, max=60)` over 6 attempts can produce, so retry backoff is a live
> explanation and not an excluded one. The accessor is fixed at all three call
> sites; **`eval/rerank-eval.jsonl` and `eval/selector-eval.jsonl` were written
> by the broken one, so their `attempts` columns are 1 by construction** and
> re-running either sweep is what would settle this. The stall/retry distinction
> stays open; only the p50 is quotable either way.

### Cost, and the one rate in this repo with no first-party source

23 questions, **23.0 billed search units**, one per question — read off
`meta.billed_units.search_units` on each response rather than inferred from the
document count, which matters because Cohere splits documents over 500 tokens
into extra units and this corpus has chunks up to 864 tokens.

At $2.00 per 1,000 searches that is **$0.046 for the sweep, $0.002 per question**
— by far the dominant per-query cost on the vector route, against $0.0000032 for
the embedding and $0.00 for routing.

**That rate is the weakest number in `src/config.py` and should be read as
provisional.** Checked 2026-08-03: `cohere.com/pricing` publishes only a Model
Vault hourly rate for Rerank 3.5 ($5/hr) and `docs.cohere.com` states no price at
all. Cohere's page does define the unit — *"A single search unit is defined as one
query with up to 100 documents to be ranked"* — but not what it costs. $2.00/1K
is what third-party pricing aggregators carry. Because the artifact stores
`search_units`, correcting the constant re-prices every historical measurement
without re-running anything.

`rerank-v3.5` was checked for deprecation at the same time and is current: the
live response carries `is_deprecated=None` and no warnings, and Cohere's
deprecations page lists only the v2.0 rerank models. Third-party listings
describing a `cohere-rerank-3.5` retirement in August 2026 refer to the
Bedrock/Pinecone-hosted naming, not this API model.

### A free observation for Step 6's refusal path

The two rows with no gold — one `out-of-scope`, one `unanswerable` — were
reranked anyway, since they cost nothing extra. **The cross-encoder is confident
on both**: top relevance **0.747** for `oos-001` and **0.796** for `oos-002`,
against 0.90 for a well-answered question. A rerank score is therefore *not* a
usable refusal signal — there is no threshold that separates these two from a
real answer. n=2, so this is an observation and not a rule, but Step 6 should not
plan on rerank confidence as a refusal input.

### Defects found on the way in

| Defect | Symptom | Fix |
|---|---|---|
| The reranker had no retry, unlike every other API call site in the repo | the first sweep died mid-run on a 429 from a 10-calls/minute trial key | `_rerank_call` wrapped in the same tenacity policy as `embedder._embed_call`; a transient limit no longer fails a request |
| The sweep embedded each question separately | 46 API calls where 24 would do, and the k-matrix arms could have seen different vectors | one batched `embed_texts` call, vectors handed to `retrieve_detailed(vector=...)` |
| Rerank p95 read 83 s and looked like a model figure | retry-vs-stall was indistinguishable in the artifact | `attempts` recorded per call; stalls listed by name, never averaged into a percentile. **The `attempts` half of this fix did not work — see the correction above; the stalls-by-name half did** |

## Graph path: template selection, execution, and prose

The router sends **10 of 23** rows to the graph (9 `both`, 1 `graph`); `3h-002`
carries `expected_fail` (ADR-0007) and drops out, so **9 rows are scored**. Two
selector arms were built and measured, as ADR-0012 did for routing: deterministic
rules over the linked entities, and Command R7B choosing a template name and
filling its parameters, which is ADR-0002's literal wording.

| arm | gold hits | rows with a hit | calls | 0-row calls | cost | p50 |
|---|---|---|---|---|---|---|
| **rules** (adopted) | **24 of 32** | 8 of 9 | 18 | 2 of 18 | **$0.00** | **5.7 ms** |
| R7B | 14 of 32 | 4 of 9 | 9 | **4 of 9** | $0.000159 | 951 ms |
| *oracle* | *24 of 32* | — | — | — | — | — |

Gold yield is the headline: does the executed plan's provenance contain the row's
gold `source_chunk_ids`? It is the same ground truth Step 4 scored the vector path
on, and unlike the metric this step was handed it is not gameable by a constant.

### The metric the plan specified does not work, and that was measured first

The phase plan says selection accuracy is measurable against each row's
`ontology_edges`. It is computable and it is nearly uninformative. Measured
**before either arm existed**:

| | value |
|---|---|
| a template traverses a declared edge | **9 of 9** |
| the linker can fill that template | **9 of 9** |
| both hold, for the same template | **9 of 9** |
| `always-obligations_for_system`, by edge-intersection | **8 of 9** |
| `always-obligations_for_role` | 7 of 9 |

All three figures are **recomputed by `scoreboard()`** from the artifact and asserted by
`test_the_ceilings_and_oracle_are_what_the_docs_claim`, the same arrangement `test_reranker.py:298`
has for its caps and oracle. For one commit they were hand-typed literals in `src/`; that is recorded
in the failure-notes addendum for this step.

Ceiling 9, floor 8 — about one row of discriminating power. `obligations_for_role`
and `obligations_for_system` between them traverse `APPLIES_TO`, `IMPOSES` and
`CLASSIFIED_AS`, which nearly every row declares, so a selector that emits one of
them unconditionally scores 89%. This is precisely the shape ADR-0012 caught R7B
with — a classifier beaten by the majority-class constant — and the only reason it
is a caveat here rather than a headline is that the constants were computed before
an arm was written. The figure is still reported, always beside its constants.

Six of the ontology's 13 relation types are traversable by **no typed template**:
`REFERENCES`, `LISTED_IN`, `SETS_PENALTY`, `EXEMPT_FROM`, `PERMITS`, `GRANTS`.
`REFERENCES` and `LISTED_IN` are the #2 and #4 most-declared edges on the eval set.
`path_between` is `[*..4]` and untyped, and is their only cover.

### The rules reach the oracle, so selection is not the binding constraint

The oracle — the best single `(template, anchor)` pair per row, chosen with the
gold visible — is **24 of 32**, and the rules arm returns exactly that. It is
allowed up to 3 calls per question and matched the best single call without
beating it, so combining templates bought nothing on this set.

That makes the constraint what the six templates can reach at all, not which one
gets picked. It is the same shape as Step 4's finding that ranking rather than
candidate retrieval bound the vector path — and it means a better selector is not
the lever here. The missing 8 chunks sit behind `REFERENCES` and `LISTED_IN`.

### `ag-001` is the row the graph exists for

| row | stratum | gold | rules | what the vector path can do |
|---|---|---|---|---|
| `ag-001` | aggregation | 11 | **11** | top-10 similarity cannot return 11 rows |

Per stratum, gold hits against gold available:

| stratum | rules | R7B |
|---|---|---|
| aggregation | **13 / 13** | 11 / 13 |
| two-hop | 7 / 10 | 2 / 10 |
| cross-regulation | 3 / 6 | 1 / 6 |
| three-hop | 1 / 3 | 0 / 3 |

Two-hop and aggregation are the two strata ADR-0001's argument rests on, and they
are the two the graph path does best on. **This is not yet a comparison** — Phase
5 runs the three-way benchmark, and these numbers are provenance coverage, not
answer accuracy.

### R7B's failure is parameter values, not template choice

This is the finding, and it is sharper than "the small model was worse". R7B
generally picked reasonable templates. What it could not do is fill them:

| what R7B produced | rows returned | the key the graph holds | rows |
|---|---|---|---|
| `system_type=high-risk AI system` | **0** | `high risk ai system` | 169 |
| `article=gdpr` | **0** | `GDPR` | 29 |
| `system_type=narrow procedural task` | **0** | — | — |

**4 of its 9 calls matched no node.** Those calls all pass `validate()`, because
validation checks parameter *names* and the graph matches parameter *values* —
ADR-0002's boundary is doing exactly its job and is silent on this by design. The
model produces display-form English; the graph is keyed on `normalize()` output,
lowercased and de-hyphenated except where `ABBREVIATIONS` forces uppercase. That
mapping is not guessable from the question, which is the entity linker's whole
reason to exist.

R7B also emitted two calls mixing parameters from two templates
(`obligations_for_role role=importer system_type=high-risk AI system`) — rejected
by `validate()`, which is the first time that guard has fired on real model output
rather than on a synthetic injection test.

### The model arm is not reproducible, and the rules arm has a test proving it is

Two sweeps of the same 23 questions at `temperature=0, seed=42` returned **16 then
14** gold hits, with the plans differing on several rows: Cohere's `seed` is
best-effort. The rules arm is pinned by
`test_the_rules_arm_still_reproduces_the_artifact`, which re-derives every plan
and compares byte-for-byte. A number that moves between identical runs cannot
anchor an ADR, so ADR-0013 quotes the committed artifact and states the spread.

### What actually reaches a prompt

| | rules | R7B |
|---|---|---|
| rows answerable | **9 of 9** | 5 of 9 |
| Neo4j rows returned | 1,684 | 332 |
| **statements rendered** | **2,886** | 617 |
| carrying an Annex VIII/XI caveat | 6 | 0 |
| built on a derived edge | **0** | 0 |

Statements, not rows, is the honest unit. The 169 rows of
`obligations_for_system('high risk ai system')` carry **one** classification fact
asserted by 124 chunks, and per-leg rendering emits it once — reporting 169 would
be the hot fact restated as a result, which is what `graph-load.md:225` warned
this step about. The live check: 169 rows → 316 statements → **1** classification
statement, citing 3 provisions and saying `+121 more`.

**The derived flag is inert on this eval set and is reported as inert.** It works —
`AIA Annex VIII interacts with GDPR Art. 35` renders `derived=True` beside the
asserted `interacts with GDPR` at `derived=False`, from the same chunk — but no
selected plan over these 23 questions traverses one of ADR-0010's 22 bridges. A
test asserts the zero, so the day a question reaches one, it has to be written
down rather than absorbed. Same treatment ADR-0012 gave the router's inert R1.

### Defects found on the way in

| Defect | Symptom | Fix |
|---|---|---|
| The selector shipped with no retry, unlike every other API call site | `th-004` came back a 429 from a 20-calls/minute trial key, scoring the arm under measurement a zero that was the key's fault | `_chat_call` wrapped in the same tenacity policy as `embedder._embed_call`; `attempts` recorded per call and excluded from the p50 |
| `_report` printed "Ceiling 10 of 9" | the routed-set ceiling (10 rows) divided by the scored-set denominator (9 rows, after `expected_fail`) — a ratio above 1 that reads as slack | every constant restated on the scored set; the routed-set figures live in the module docstring where nothing can divide them |
| The plan asserted `RiskCategory` over-claims in `router.ANCHOR_TYPES` | it does not — `definition_of`'s `(t:Entity)` head accepts it | the disagreement is one-directional and is 6 types the router *excludes* that do fill a parameter; pinned by a test |
| The pre-registered ceiling and oracle were computed outside the repo | three scripts in a temp directory, transcribed into `src/` as literals; `test_rules_reaches_the_oracle` compared a constant to itself | `scoreboard()` computes all three; no literal remains in `src/`; `test_no_arm_exceeds_the_oracle` makes the invariant structural; `--rebuild` replays the model half and recomputes the graph half with no key |

## Open

- ~~**The graph path emits 2,886 statements over 9 questions — ~320 per question —
  and Step 6 cannot put that in a prompt.**~~ **CLOSED by ADR-0014** (Step 6). All
  three options named here were built and measured — rerank the statements,
  interleave per template call, score by anchor — against the constant `docs[:50]`
  and an uncapped ceiling. **The constant won, and every ranked arm was actively
  worse**: reranking and anchor-scoring both concentrate near-duplicate statements,
  and Command A responds to a monotone document set by writing `<co>` citation
  markup into the answer until `max_tokens`. The cap costs 15 of 25 reachable gold
  chunks and the best-to-worst spread between capped arms is 6, so the budget is
  the lever and the ranking inside it is nearly noise. See
  `docs/metrics/answer-path.md`.
- **The oracle is the constraint, not the selector.** The rules arm already
  reaches the best single call per row. The 8 unreached gold chunks sit behind
  `REFERENCES` and `LISTED_IN`, which no typed template traverses. A seventh
  template — or a typed `path_between` — is the lever, not a smarter selector.
- **`router.ANCHOR_TYPES` excludes 6 types that do fill a declared parameter**:
  `Authority`, `DefinedTerm`, `LawfulBasis`, `Penalty`, `Regulation`, `Right`. Its
  comment says "none is a parameter any template declares", which is true of the
  three typed templates and false of `definition_of`, whose head is `(t:Entity)`.
  The router is **deliberately unchanged** — ADR-0012's adopted 21 of 22 was
  measured with the current set, and editing it silently re-measures Step 3. A
  step that can afford to re-run the router sweep should widen it and see whether
  R2 still fires the same way.
- **Every number here is provenance coverage, not answer accuracy.** A gold chunk
  appearing in a statement's citation list is not the same as the answer being
  right. Phase 5's judge is what closes that gap.
- **The graph path's statements carry no statute text.** A `ContextDoc` from this
  path holds the rendered statement and a citation label, by decision. On `both`
  the passage text arrives via the vector path; on `graph` — `ag-001` alone —
  Command A will see statements and no legislative prose. Whether that is enough
  to generate from is a Step 6 question and is untested here.
- **`path_between` is measured at zero.** It is the only cover for the 6
  untraversable relation types and the rules arm never fired it: S6 only triggers
  when nothing else matched, and something else always matched. Its provenance is
  also one arbitrary chunk per hop where parallel edges exist.
- **Rerank leaves 10 of 13 available chunks unrecovered**, and nothing here
  explains why. The candidate pool is not the constraint — that is measured — so
  the next lever is chunking (ADR-0003) or a different ranking signal, not a
  bigger `top_k`.
- **No per-stratum rerank claim is supportable at n=23.** Every stratum delta is
  0, +1 or +2 against a pre-registered resolution of ±2. The 100-row set is what
  would make two-hop's +1 either a finding or a nothing.
- **The rerank rate has no first-party source.** See above. `search_units` is
  measured; the dollars are not.
- **`AskResponse.cost_usd` is `float` and required (`src/schemas.py:294`).** It
  can now be filled for the vector route, but only because `RERANK_PRICE_PER_SEARCH`
  is a number rather than `None`. If that constant ever goes back to `None` — the
  honest state if the aggregator figure is withdrawn — the field cannot represent
  the route's cost. Step 7 has to decide between `float | None` and a
  "priced components only" flag; this is flagged now so it is not discovered then.
- **Rerank latency is unmeasured on a production key.** 286 ms p50 comes from 20
  clean calls on a trial key whose tail behaviour is visible in the stalls above.
- **The rules number is in-sample and the eval set cannot currently say by how
  much.** Nothing here is a held-out measurement. The 100-row set is the only
  thing that resolves it, and the honest prior is that 95% will fall.
- **`th-004` is unrepaired on purpose.** One row is not enough evidence to add a
  rule, and the repair that fixes it breaks `oos-002`. It stays a recorded miss.
- **`both` has exactly one producer (R4) and it is a regular expression over
  English question shape.** A question phrased as two sentences rather than one
  conjoined clause — "A deployer fails its Article 26 obligations. Which penalty
  applies?" — is invisible to it, which is `th-004`. The 100-row set should
  deliberately include that phrasing.
- **Adopting rules gives up the roadmap's confidence heuristic.** *"Below a
  confidence heuristic (or on `both`), run both paths"* assumes a scored
  classifier. The linker offers a free substitute (count and type of anchors) and
  it has not been needed.
- **The ambiguity arm is a no-op on this eval set, and that is not the same as
  ambiguity being handled.** 372 alias surfaces name more than one node, but
  after the widened plural fold and canonical-before-alias ordering, **0 of 79**
  links over 23 questions come out ambiguous — so "every link" and "unambiguous
  links only" report identical tables. The policy (emit all candidates, flagged)
  is therefore *untested by measurement*. It needs the eval set at 100 rows
  before it means anything.
- **A multi-word possessive does not link.** `"the European Commission's report"`
  reaches neither `european commission` nor the report, because the fallback is
  restricted to single-token spans. Proper leftmost-longest *cover* — scoring
  parses by tokens consumed rather than committing left to right — would fix this
  and the shadowing case together. Deferred: no eval row exercises it.
- **Generic `DefinedTerm` nodes are linked and mostly are not wanted.**
  `authority`, `processing`, `product`, `infringement`, `conformity assessment`
  are real nodes that a question names in passing. No stop-list was adopted,
  because `ai system` is in the same shape and *is* a gold entity for `3h-002`, so
  a blanket filter would cost real links. Any filter needs a stated rule and a
  measured cost, not a hand-picked list.
- ~~**Precision has no upper reference.** 52% (64% excluding instruments) is the
  first measurement of anything on this path; nothing says whether it is good.
  Step 5's template-selection accuracy against `ontology_edges` is the first
  number that will constrain it, because a wrong link there is a zero-row query
  rather than a slightly worse prompt.~~ → **Answered, and not by the number this
  bullet expected (2026-08-04).** `ontology_edges` accuracy turned out to have a
  ceiling of 9 of 9 and a constant floor of 8 of 9, so it constrains nothing. The
  number that does is the one it predicted the *shape* of: **2 of the rules arm's
  18 calls matched no node**, which is linker precision expressed as zero-row
  queries rather than as a percentage — and 4 of 9 for R7B, which is what the
  linker is worth. Linker precision still has no upper reference; what it now has
  is a cost, in queries that validate and return nothing.
- **The 23-row eval set is the limit on all of the above.** Coverage is 40 gold
  chunks over 1,108 — **3.6%** — and only 6 of 40 are GDPR-side
  (`eval-set.md`). Two strata are 2 rows each, so a single question moves them 50
  points.
