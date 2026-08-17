# Query path metrics

Status: **Entity linker, router and vector path built and measured. 23 of 23 eval
questions link to at least one graph node; 52% of links land in a gold chunk's
entity set (64% excluding instrument nodes). Routing is deterministic: 21 of 22
rows, $0.00, 3.5 ms — Command R7B scored 10 of 22, below the majority-class
constant, and never emitted `both` at all. **Rerank 3.5's aggregate gain did not
survive the 100-row eval set: +4 at k=5 became +2, which is inside ADR-0004's
resolution, and the paired test puts it at 16 wins / 13 losses / 61 ties,
p = 0.711 — no measured difference at the cap that ships.** Of the gold that
never reaches the prompt, **46 references are outside the candidate pool, 6 are
lost to the passage cap and 51 are ranked below noise**, so ranking is the
binding constraint and the cap binds on no stratum but aggregation. A BM25 +
full-text union lifts the pool from 157/203 to 176/203 — the largest retrieval
gain measured here — and delivers **+2 into the prompt**, so it is committed and
switched off. **Using structure to reorder the pool rather than add to it fails
too**: article diversity, graph-connectivity boosting and inbound `REFERENCES`
were measured offline against the committed pools and the best of them is +2
chunks at p = 0.500. Six levers, none of which moves the ranking stage. **The
seventh does not try to: deterministic enumeration reads a whole provision in
statutory order on the questions that ask for one, and takes aggregation gold in
the prompt from 12/48 to 33/48** — at 1.2% false positives on the other 80 rows
and no change to any route. End to end, **enumerating the provision ALONE takes
aggregation from 2/10 to 4/10, 2 wins and 0 losses, at lower cost and latency
than the arm it replaces**; `ag-006` turned out to fail because Article 100 was
in its ranked top-5, not because synthesis confused Article 99's paragraphs.
Decomposed synthesis was built for that row, is 2.0x cost for zero verified
rows, and is not adopted. The
graph path now runs end to end: deterministic template selection reaches **24 of
32 gold chunks — the oracle exactly** — against R7B's 14, at $0.00 and 5.7 ms,
and `ag-001` comes back **11 of 11** where top-10 similarity cannot return 11
rows at all.**
Phase 3 Steps 2–5 complete. **Steps 6 and 7 are measured in
`docs/metrics/answer-path.md`** — split out because this file is already 650+
lines and because generation, citation validation and the statement budget are a
different subject from retrieval. **Step 7's live per-route latency and cost
table is there**, beneath the replayed sweep table it corrects: 23 of 23
questions through `POST /ask`, $0.0067 median per question, 3.4 s pooled p50.

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

> **n=23, SUPERSEDED.** Everything from here to §Latency was measured on the
> 23-row eval set (21 scoreable queries, 51 gold references). The set is now 100
> rows / 90 scoreable / 203 gold, and the re-measurement reverses the headline:
> **the +4 at k=5 below does not survive, and rerank is not distinguishable from
> the raw vector draw at the cap that ships.** These tables are kept because the
> comparison is the interesting part and because ADR-0004's resolution rule was
> pre-registered against them. Read
> §[Vector path, re-measured at 100 rows](#vector-path-re-measured-at-100-rows-2026-08-16)
> for the current numbers.

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

> **This subsection's title is the one claim here that got STRONGER at 100 rows,
> and its body is the one that got weaker.** The title holds and is now
> quantified: of the gold that never reaches the prompt, 6 chunks are lost to the
> passage cap and 51 to ranking. The body does not: at 100 rows the pool holds
> only 157 of 203, so "the candidate pool is not the constraint" is true of the
> narrowing stage and false of retrieval overall — 46 gold references are
> unreachable at any k ≤ 50. Both halves are measured below.

### Vector path, re-measured at 100 rows (2026-08-16)

The eval set went from 23 rows / 51 gold to 100 rows / 203 gold (`eval-set.md`),
so every number above HAD to move. What did not have to move is the shape, and
the shape moved too. All of this is recomputed from `eval/rerank-eval.jsonl` by
`python -m src.query.reranker --eval` — pure, no key, no containers.

#### The k-matrix, and the gain that stopped clearing the resolution

| k | pre-rerank | post-rerank | delta | ceiling | top-50 oracle | hit rate pre → post |
|---|---|---|---|---|---|---|
| 5 | 98/203 — 48.3% | **100/203 — 49.3%** | **+2** | 190/203 | 151/203 | 80.0% → **90.0%** |
| 10 | 117/203 — 57.6% | **123/203 — 60.6%** | **+6** | 202/203 | 157/203 | 90.0% → 93.3% |
| 50 | 157/203 — 77.3% | 157/203 — 77.3% | 0 | 203/203 | — | 96.7% → 96.7% |

**At k=5 — the cap that actually ships — the gain is +2 chunks, which is INSIDE
ADR-0004's ±2 resolution.** At 23 rows it was +4 and cleared. The paired test
says the same thing without relying on an aggregate: rerank vs the raw vector
draw is **16 wins / 13 losses / 61 ties, exact McNemar p = 0.711**. There is no
measured difference at k=5. `test_the_aggregate_gain_at_k5_does_NOT_clear_the_resolution`
pins the inverted claim; the old test asserted the opposite and was changed
deliberately rather than repaired.

#### Per stratum

| Stratum | n | gold | pre@5 | post@5 | Δ | pre@10 | post@10 | Δ | paired @5 (w/l) | p |
|---|---|---|---|---|---|---|---|---|---|---|
| single-hop | 20 | 24 | 21 | 20 | −1 | 22 | 22 | 0 | 1/2 | 1.000 |
| two-hop | 20 | 42 | 24 | 24 | 0 | 26 | 29 | +3 | 3/3 | 1.000 |
| three-hop | 15 | 44 | 17 | 17 | 0 | 24 | 21 | −3 | 3/3 | 1.000 |
| cross-regulation | 15 | 32 | 12 | **17** | **+5** | 15 | 19 | +4 | **5/0** | 0.062 |
| aggregation | 10 | 48 | 15 | **12** | **−3** | 19 | 20 | +1 | 2/4 | 0.688 |
| hard-negative | 10 | 13 | 9 | 10 | +1 | 11 | 12 | +1 | 2/1 | 1.000 |

The n=23 section above said no per-stratum claim was supportable. Two now are,
and **they point in opposite directions**: cross-regulation is the reranker
earning its keep (5 wins, **zero losses**, and the same sign at k=8 and k=10),
aggregation is it costing recall. `vector-index.md` §Open nominated Rerank 3.5 as
"the obvious lever on the two-hop figure" — two-hop is 3/3, dead even.

So the deletion question does not have a clean answer. Rerank nets to noise
overall while helping one stratum and hurting another, at 9.4 s p95 and
$0.0024/query.

#### Where the gold goes: retrieval 46, cap 6, ordering 51

Three disjoint causes that sum to `gold − post@k`, derived by `scoreboard()` and
printed by `--eval` so they cannot drift from a doc:

| Stratum | gold | not retrieved | over cap | mis-ordered | reached |
|---|---|---|---|---|---|
| three-hop | 44 | 11 | 0 | 16 | 17 |
| aggregation | 48 | 17 | **6** | 13 | 12 |
| two-hop | 42 | 7 | 0 | 11 | 24 |
| cross-regulation | 32 | 11 | 0 | 4 | 17 |
| single-hop | 24 | 0 | 0 | 4 | 20 |
| hard-negative | 13 | 0 | 0 | 3 | 10 |
| **ALL** | **203** | **46** | **6** | **51** | **100** |

**Ordering costs 8.5× what the cap costs, and the cap costs nothing at all
outside aggregation.** Five of six strata have gold counts at or under 5, so
`PASSAGE_TOP_N = 5` cannot lose them anything; only 4 rows in the whole set need
more than 5 chunks (7, 7, 8, 11 — all aggregation). A perfect reranker at k=5
would reach 151/203; it reaches 100.

That is the answer to "is the passage cap too tight for multi-hop": **no, and not
marginally — the cap is not binding on multi-hop at all.** Raising
`PASSAGE_TOP_N` would move the 6-chunk term. `tests/test_reranker.py:LOSS` pins
the decomposition and `test_the_cap_binds_only_on_aggregation` pins the
per-stratum shape.

On the 37 rows where gold was displaced out of the top 5, **4.03 of the 5 slots
are non-gold on average**, and on 6 rows all five are. `th-011` is the clean
case: gold `gdpr-art15-para1` sits at rerank rank 30 with score 0.141 while
`gdpr-art12-para2` (0.535) takes slot 1. 13 of the 51 mis-ordered chunks are
displaced by a sibling paragraph of the same article — `aia-art6-para2` is
outranked by `aia-art6-para3` on three separate questions — which is a
chunking-shaped failure (ADR-0003), but it is a quarter of the total, not most
of it. On the union pool the same count is **11 of 66 (16.7%)** — see §Structure
as a reordering signal, where a diversity constraint built to exploit exactly
this fails, because on 21 of 90 rows the gold *is* a sibling cluster.

#### Rerank 3.5 is deterministic here. The embedding is not.

Two full `--eval --refresh` sweeps of an unchanged corpus, differenced:

| | agreement between runs |
|---|---|
| rerank top-5 set and order | **100 / 100 rows** |
| rerank relevance scores | 99.1% byte-identical (max Δ 0.00087, max rank move 1) |
| candidate pool **membership** | 93 / 100 rows |
| candidate pool **order** | 78 / 100 rows |

This inverts the natural assumption. The cross-encoder is stable; `embed-v4.0`
does not return byte-identical vectors, and the churn lands at the rank-50
boundary. It moved `PRE[5]` from 97 to 98 — on `3h-015`, `aia-annex3-point1`
displaced `aia-art26-para11` from slot 5 — so that constant is now a membership
test (`PRE_5_UNSTABLE`) rather than an equality one. Every other pinned value
held on both sweeps, including the whole 46/6/51 decomposition.

Consequence worth stating plainly: `delta@5 = post@5 − pre@5` inherits the
instability of its subtrahend, so a ±1-chunk aggregate delta at k=5 is not a
measurement of anything.

#### The lexical union: +19 into the pool, +2 into the prompt

Postgres full-text search and an in-process Okapi BM25 (`src/query/lexical.py`,
k1=1.2, b=0.75) were added as a **recall device**, unioned into the candidate
pool with no score fusion, and the existing reranker kept as the ranking stage.

Every lexical arm and every fusion **loses** as a ranker, which is why the union
is a union and not an RRF:

| arm | recall@5 | paired vs rerank (w/l/tie) | p |
|---|---|---|---|
| rerank 3.5 (shipping) | 49.3% | — | — |
| vector only | 48.3% | 13/16/61 | 0.711 |
| BM25 only | 37.4% | 7/30/53 | <0.001 |
| pg_fts only | 10.3% | 2/67/21 | <0.001 |
| RRF(vector, BM25) | 45.3% | 9/17/64 | 0.169 |
| RRF(vector, pg_fts) | 30.5% | 8/42/40 | <0.001 |

As a recall device it does exactly what it was built to do — and almost none of
it survives the ranking stage:

| | vector pool | union pool | delta |
|---|---|---|---|
| gold in pool | 157 | **176** | **+19** |
| gold in top-5 | 100 | **102** | **+2** |
| → retrieval loss | 46 | 27 | −19 |
| → cap loss @5 | 6 | 8 | +2 |
| → **ordering loss @5** | 51 | **66** | **+15** |

**The union moved gold from a stage that could not reach it to a stage that ranks
it below noise.** Paired at k=5 it is 2 wins / 0 losses / 88 ties, p = 0.500.
Both lexical arms are needed for the +19 (BM25 alone is +7, pg_fts alone +14);
they recover different things — pg_fts finds six of Annex III's eight
near-identical enumerated points for `ag-008`, BM25 finds the Art. 99 fine tiers.

It is **committed and switched off**: `answer_path.LEXICAL_DEPTH_LIVE = 0`, held
by `test_the_union_is_measured_but_off_in_the_live_path`. What it would cost:

| | before | after |
|---|---|---|
| documents sent to rerank | flat 50 | p50 106, p95 125, max 132 |
| pools over Cohere's 100-doc search unit | 0% | **66%** |
| billed search units / 100 questions | 100 | 198 (**1.98×**) |
| rerank $/query | $0.0020 | $0.0040 |
| rerank latency p50 | 264 ms | 386 ms |
| lexical retrieval | — | +115 ms p50 |

2× the rerank bill and ~240 ms for +2 gold chunks is not a trade worth making
while ordering is the constraint. Depth is the lever if it is ever revisited:
recovery is back-loaded (+2 at depth 5, +9 at 20, +13 at 30, +19 at 50) and
**depth 30 keeps every pool under 100 documents**, so it buys +13 at unchanged
rerank billing.

#### Structure as a reordering signal: three interventions, all negative

Every lever tried before this ADDED material to the pipeline and failed the same
way — graph statements (0 wins, 4 losses), cap 5→8 (+6 chunks, ~80% noise), the
lexical union (+19 pool, +2 prompt). This session tested the opposite: use
structure to **reorder and filter the existing pool**, sending no extra passages.

All three are permutations of a committed rerank ordering, so relevance is held
fixed and only the selection policy changes. Nothing here is a live measurement
and nothing cost anything. **All three failed, and the three failures have three
different causes**, which is the useful part.

**1. Article diversity — no variant wins on any stratum.**

| variant | recall@5 | vs baseline | w/l/tie | p |
|---|---|---|---|---|
| plain rerank (baseline) | 49.3% (100) | — | — | — |
| cap 2 per article | 48.3% (98) | −2 | 0/2/88 | 0.500 |
| cap 3 per article | 48.8% (99) | −1 | 0/1/89 | 1.000 |
| cap 4 per article | 48.8% (99) | −1 | 0/1/89 | 1.000 |
| MMR, λ=0.9 | 46.3% (94) | −6 | 3/9/78 | 0.146 |
| MMR, λ=0.7 | 41.9% (85) | −15 | 4/18/68 | **0.004** |

**Zero wins for the hard cap, on any stratum, at any N** — the per-stratum table
is flat or down in every cell. The reason is that sibling clustering is not
purely a failure mode: **21 of 90 rows have gold containing two or more
paragraphs of a single article, including 6 of the 10 aggregation rows.** A
diversity constraint cannot tell "Art. 26(5) and 26(6) are both the answer" from
"Art. 6(3) is crowding out Art. 6(2)", so it removes gold and noise at
comparable rates. MMR is worse than the hard cap because it applies the penalty
everywhere rather than only past a threshold.

**The sibling-displacement fraction FELL on the union pool, against expectation.**
It was 13 of 51 (25.5%) on the vector pool; on the union pool it is **11 of 66
(16.7%)** — down in the fraction and down in the absolute count. The +19 was
mostly enumerated siblings, so the prediction was that it would rise. What the
lexical union actually added was competition from *unrelated* provisions: the
non-sibling term went 38 → 55. Sibling confusion is a real but shrinking
minority of the ordering problem, and that is an argument against reading the
ordering loss as a chunking problem (ADR-0003).

**2. Graph-connectivity boost — the signal is not discriminative.**

Coverage first, as the gate: **90 of 90 scoreable questions resolve at least one
entity** (mean 3.0, `entity_linker.link_detailed`), and 1,107 of 1,108 chunks
carry `entity_ids`. Reach is not the problem. Discrimination is:

- 1 hop from an anchor: **66.2%** of the top-50 pool is already "connected"
- 2 hops: **84.6%**

A boost that marks two-thirds to five-sixths of the candidates is close to a
constant, and where it is not, it mostly demotes the unconnected third — which
contains gold at roughly the pool's base rate. Sweeping the bonus:

| bonus | 1-hop | 2-hop |
|---|---|---|
| +0.05 | +0 (0/0/90) | **+2** (2/0/88, p=0.500) |
| +0.10 | +0 (0/0/90) | +2 (2/0/88) |
| +0.20 | −2 (0/2/88) | −1 (2/3/85) |
| +0.30 | −4 (0/4/86) | −3 (1/4/85) |
| +0.50 | −10 (0/10/80, p=0.002) | −10 (0/9/81, p=0.004) |

The best cell is +2 chunks at p=0.500 — inside ADR-0004's resolution, i.e. no
measured difference. Everything beyond a token bonus is monotonically harmful.
The graph never entered the prompt in any of these arms; it was a scoring signal
only, which is the one configuration the four prior graph measurements had not
tried, and it does not work either.

**3. Inbound `REFERENCES` (E3, first run) — the reach is 15 of 90.**

The traversal is `(p)-[:REFERENCES]->(:Article {canonical_name: $a})`, reading
`r.source_chunk_id`; 1,161 such edges are loaded and no template has ever
traversed them. The question asked was deliberately narrow: not how much the
pool gains, which the last session showed does not convert, but **how many gold
chunks already in the union pool but ranked below 5 does it promote.**

On the five rows nominated for it — `th-006`, `th-004`, `th-005`, `3h-005`,
`ag-006` — 7 gold chunks are addressable (in pool, rank > 5). A linker-anchored
traversal recovers **1**. An oracle anchor taken from the gold citations — a
ceiling, not deployable — recovers **3**. Three of the five rows resolve no
Article or Annex entity at all, because the question never names an article
number: `3h-005` ("an employer deploys an AI system to evaluate candidates") and
`ag-006` ("what are the AI Act's fine tiers") are topical, not referential.

Corpus-wide that is the binding limit. **Only 15 of 90 scoreable questions
resolve an Article/Annex anchor** — 0 of 15 cross-regulation rows do — against
74 addressable gold chunks, of which the traversal recovers **4** (`th-006`,
`th-015`, `3h-002`, `hn-008`; three of them the same chunk, `aia-art6-para2`).
Applied as a hard promotion: 102 → 103 of 203, **net +1, 2 wins / 1 loss,
p = 1.000**.

`aia-art99-para4` is the one genuine success and it is worth naming, because it
is exactly the case the E3 hypothesis was written for: `th-006` asks which
penalty tier an Art. 16 breach falls into, the edge `aia art. 99(4)
-REFERENCES-> aia art. 16` exists, and the traversal pulls `aia-art99-para4`
from rank 10 into the prompt. The mechanism works. It fires four times in ninety
questions.

**Read together:** none of the three clears the +5 bar this session set, and
none clears ADR-0004's ±2 either. The best result across every variant tested is
+2 chunks at p = 0.500. Reordering the pool with structure does not recover the
51 mis-ordered chunks, for three separate reasons — diversity cannot distinguish
gold siblings from noise siblings, connectivity marks most of the pool, and
reference traversal reaches a sixth of the questions. That the causes are
independent is what makes this a stronger negative than three variants of one
idea would have been.

#### Deterministic enumeration: the first lever that moves retrieval

Six levers had failed by this point and all six tried to make the ranking stage
better, either by giving it more to rank or by reordering what it had.
Enumeration does neither. On questions whose answer is *every limb of one
provision*, it **removes ranking from the path** and reads the article in
statutory order — the one ordering that is correct by construction.

**The shape of the stratum is what makes this possible.** 21 of 90 rows have gold
spanning two or more paragraphs of a single article, including 6 of 10
aggregation rows; `ag-001`'s gold is all eleven substantive paragraphs of Article
26. Asking a cross-encoder for "the five paragraphs that answer this" is the
wrong question when the answer is twelve paragraphs long.

**What was built.** `retrieve_by_article(regulation, article)` and
`retrieve_by_annex(regulation, annex)` (`src/query/retriever.py`), ordered by
`paragraph` / `section, point`; the composite indexes `chunks_article` and
`chunks_annex` that `schema.sql:31-33` never had; and a `MAX_ENUMERATION = 16`
bound. The bound matters: `aia-art3` is the definitions article at **68**
paragraphs, and enumerating it would put more in a prompt than the graph budget
ever did. Over the bound the function returns `[]` and ranking stands — a
truncated enumeration is worse than none, because half of Article 3 is an
arbitrary prefix that reads as complete.

**The detector, and the precision it was specified for.** A false negative costs
nothing; a false positive puts a whole article into a prompt that did not ask for
one. So it fires on three narrow shapes — `^List`, `each`, and an annex named
with `cover/areas/list`:

| detector | fires on aggregation | false positives / 80 | FP rate |
|---|---|---|---|
| `List` only | 2 / 10 | 0 | 0.0% |
| `List` \| `each` | 3 / 10 | 0 | 0.0% |
| **adopted** (+ annex-cover) | **4 / 10** | **1** (`hn-008`) | **1.2%** |
| + plural head noun | 8 / 10 | 4 | 5.0% |
| both of the above | 8 / 10 | 5 | **6.2%** |

The wider variants were measured and rejected against the ~5% stop condition.
They cannot separate `ag-001` "List the main obligations the AI Act places on
deployers" (11 gold chunks) from `sh-019` "What documentation obligations does the
AI Act place on providers" (1 gold chunk) — nothing in the question shape
distinguishes them, and the second is not an enumeration question.

**ADR-0012 IS NOT RE-MEASURED, and structurally rather than by luck.**
Enumeration is a flag on `RouterResult`, not a sixth rule: the five rules and the
route they return are untouched. Verified on all 100 rows — **0 route changes, 0
rule-name changes** against the committed rules arm, asserted by
`test_enumeration_does_not_change_any_route`. Writing it as `R0` would have
forced it to pick a route as well, and every choice was wrong: `graph` sends it
somewhere being cut, `vector` overrides R4's two-hop finding on a conjoined
question.

**Two design choices that were measured, not assumed.**

*Augment, do not replace.* Substituting the enumeration for the ranked top-5
reaches 23 of 48 aggregation gold; keeping both reaches **27**, and no row can
lose gold it already had. Under replacement `ag-004`, `ag-005` and `ag-010` are
each worse, because the chosen article is not always where the gold is. This is
also what makes the one false positive cheap: `hn-008` keeps its five passages
and gains an article it did not need — it costs prompt length, not recall.

*Explicit reference beats inference.* Where the question names a provision, that
wins; otherwise the target is the modal article over the top 10 reranked
candidates (`ENUM_VOTE_N`, swept: top-1 and modal@5 reach 20 of 48, modal@10 and
modal@20 reach 23). `ag-008` is why: the inferred target is `aia-art49` and scores
**0 of 8**, the explicit one is Annex III and scores **8 of 8**.

**Retrieval result — the largest single gain measured on this corpus:**

| row | target | source | gold before → after | passages |
|---|---|---|---|---|
| `ag-001` | AIA Art. 26 | vote | 2 → **11** | 15 |
| `ag-008` | AIA Annex III | explicit | 0 → **8** | 13 |
| `ag-006` | AIA Art. 99 | vote | 1 → **4** | 14 |
| `ag-004` | GDPR Art. 21 | vote | 2 → 3 | 10 |
| `hn-008` | AIA Annex III | explicit | 1 → 1 | 13 |

**Aggregation gold reaching the prompt: 12 of 48 → 33 of 48.** Corpus-wide the
equivalent figure is 102 → 123 of 203 (50.2% → 60.6%). For comparison, the
lexical union — the previous best — moved the corpus figure by +2.

#### Enumeration end to end: 1 win, 0 losses, and not resolvable at n=10

Measured on the 10 aggregation rows, majority-of-3 per arm, paired, using the
reducer in `eval/repeat_report.py`. Three tagged sweeps of `rerank-enum`
(`enum-a/b/c`) plus a third run of each incumbent, **$0.373** total.

| comparison | incumbent | enum | B wins | B loses | McNemar p |
|---|---|---|---|---|---|
| vs `rerank` | 2/10 | **3/10** | 1 (`ag-001`) | **0** | 1.000 |
| vs `vector` | 0/10 | **3/10** | 3 (`ag-001`, `ag-002`, `ag-010`) | **0** | 0.250 |

**Say it plainly: 10 rows cannot resolve this.** One row is 10 percentage points
on this denominator, so the smallest observable difference is already twice the
±5.2pp noise band established for the 100-row cells. The result is directionally
positive with **zero losing rows in either comparison**, and that is the whole of
what it supports. It is not a demonstration that enumeration improves accuracy.

**The interesting part is the rows that did not flip.** Retrieval is now solved
on them and they still fail:

- `ag-006` — all **4 of 4** gold paragraphs of Article 99 in the prompt and cited.
  Verdict `wrong` on all three runs: the answer mis-pairs the EUR 7.5M and 15M
  ceilings with the wrong infringement categories. The correct text was in front
  of it. **→ CORRECTED the next session: this is not a synthesis failure.** The
  correct text was in front of it and so was Article 100, the fine schedule for
  Union institutions, sitting at ranks 1 and 3 of the ranked passages that
  enumeration appends. The wrong figures are copied from there. See §Synthesis on
  enumerated provisions — dropping the ranked tail fixes the row, and the
  "generation is the new constraint" reading below is right for `ag-008` and
  wrong for this one.
- `ag-008` — all **8 of 8** Annex III points in the prompt and cited, every area
  listed, nothing invented. `partially_correct`, because the grading rule also
  wants the Art. 6(2) high-risk consequence, which is in a different article the
  enumeration does not reach.
- `ag-003` — the detector did not fire, and the answer omits Art. 83(6). This one
  a wider detector would have fixed.

So the enumeration did its job and handed the failure downstream. That is
progress — the aggregation stratum's problem was retrieval and is now
generation — but it is a different problem, and the accuracy column will not move
until it is addressed. `MAX_TOKENS` at 2000 is not the constraint: **0
truncations** across all 30 enumeration generations, on prompts up to 15 passages.

#### Synthesis on enumerated provisions: the premise was wrong, and the control won

This session set out to fix `ag-006` with decomposed synthesis -- one extraction
call per enumerated paragraph, then deterministic assembly -- on the theory that
a single pass over twelve near-identical paragraphs cannot keep twelve
(subject, value) pairs straight. **The theory was wrong about this row, and
finding that out cost one control arm.**

**`ag-006` was never a synthesis failure.** Its ranked top-5 is
`[aia-art100-para3, aia-art99-para3, aia-art100-para2, aia-art99-para7,
gdpr-art83-para2]`. **Article 100 is the fine schedule for Union institutions,
bodies and agencies** -- EUR 1 500 000 and EUR 750 000 -- and those are exactly
the two figures the failing answer reports as AI Act tiers. The model was not
confusing Art. 99's paragraphs with each other; it was reading a neighbouring
article that ranking had put in front of it and enumeration had left there,
because enumeration *augments* the ranked passages rather than replacing them.

So the arm that matters is the cheap one. Four arms, majority-of-3, paired, on
the 10 aggregation rows:

| arm | passes | new win | calls/row | $/fired row | latency/fired row |
|---|---|---|---|---|---|
| `rerank` | 2/10 | — | 1 | $0.0075 | 8.4 s |
| `rerank-enum` (augment) | 3/10 | `ag-001` | 1 | $0.0106 | 8.8 s |
| **`rerank-enum-only`** (provision alone) | **4/10** | **`ag-006`** | 1 | **$0.0066** | **5.8 s** |
| `rerank-enum-decomposed` | 5/10 | `ag-008`* | **9.2** | $0.0130 | 20.5 s |

Zero losses at every step. `rerank-enum-only` vs `rerank` is 2 wins / 0 losses,
p = 0.500; vs `rerank-enum` it is 1 / 0.

**Dropping the ranked passages is cheaper than keeping them and wins the row
decomposition was built for.** It is also cheaper than the arm it replaces --
fewer passages in the prompt than `rerank-enum`, and fewer than `rerank` on some
rows -- so this is the first change in this file that improves accuracy and cost
together. The trade is real, though: `enum-only` gives up the ranked passages'
gold, which costs recall on `ag-004` (3 of 7 gold to 1 of 7) and `hn-008`. It
still wins on verdicts, because on this stratum a contaminated prompt is worth
less than a smaller clean one.

**\* `ag-008`'s win is a judge failure and should not be counted.** The grading
rule says omitting the Art. 6(2) high-risk consequence is *partial*. **None of the
three decomposed answers contains the string "high-risk" at all**, and all three
were graded `correct`; the identical omission under `enum-only` was graded
`partially_correct` 3 times out of 3. One judge reason states the answer "omits
the Art. 6(2) consequence which is treated as partial" and then returns
`correct`; another asserts it "correctly identifies the Art. 6(2) consequence",
which is false of the text. The plausible mechanism is format: the decomposed
answer is longer, clause-per-citation and densely labelled, and the judge appears
to reward that. **This is the first measured case of the judge being wrong in a
direction that favours an arm under test**, and it is a caution for every
comparison in this file, not only this one -- 85% agreement was measured on
`rerank`-shaped prose and has never been re-measured on a different answer shape.

**So decomposed synthesis buys zero verified rows for 2.0x cost and 3.5x
latency** (9.2 calls per fired row, 5.8 s to 20.5 s). By the criterion this
session set -- if it does not flip the row it was built for, drop it rather than
tune it -- it is not adopted. It is kept in the tree, tested and measurable, and
`rerank-enum-decomposed` names it in `run_benchmark.SYSTEMS`.

What the extraction *did* demonstrate is that the per-paragraph pairings are
correct when read in isolation: 35M/7% to Art. 5 prohibitions, 15M/3% to operator
obligations, 7.5M/1% to misinformation, and the SME "whichever is lower"
inversion. The mechanism works. The row did not need it.

#### The wider detector, measured end to end: a wash with two casualties

The rejected detector variant catches 8 of 10 aggregation rows against the
adopted 4, at 6.2% false positives against 1.2%. Recall alone made it look like a
trade worth revisiting once enumeration was cheap. Graded on 30 rows,
majority-of-3, it is not:

| stratum | incumbent | wide | wins | losses | p |
|---|---|---|---|---|---|
| single-hop | `rerank` 15/20 | 15/20 | `sh-019` | **`sh-010`** | 1.000 |
| aggregation | `enum-only` 4/10 | 4/10 | `ag-003` | **`ag-002`** | 1.000 |

**Net zero, and the two losses are the predicted failure mode**: rows that were
correct, flipped by added context. `ag-002` is the instructive one. It asks
"which of a deployer's obligations under Article 26 are **modified** when the
deployer is a financial institution" -- a question about *part* of a provision.
Enumerating all twelve paragraphs of Article 26 buries the two-sentence
carve-out that is the entire answer, and the row goes from `correct` to
`partially_correct` on all three runs.

That is the sharp form of the rule this whole line of work has been circling:
**enumeration is right when the question asks for all of a provision and wrong
when it asks about a part of one**, and question shape alone does not reliably
separate those. The adopted narrow detector stays.

#### Why the end-to-end measurement was not run

The obvious follow-up is a majority-of-3 A/B of the union arm through generation.
It was **deliberately not spent** (~$1.79), because the offline data bounds the
result first: **75 of 100 rows receive a byte-identical passage set** under the
two arms, so they can produce only provider noise (~5.3 expected flips at the
7.1% rate). Of the 25 rows that change, **2 gain gold** and 23 swap one non-gold
passage for another. A 2-row signal against a comparable noise floor is not
measurable at this eval-set size, and the honest move is to say so rather than
buy a null with three decimal places on it.

The instrument exists for when there is signal: `eval/repeat_report.py` now has a
majority-of-k reducer (`majority_verdicts`, `compare_arms`, exact McNemar) and a
`rerank-pool` arm is wired into `run_benchmark.SYSTEMS`, replayable from the
committed `pool_reranked` ordering.

    python -m eval.repeat_report --system rerank --tags '' e1-run-a e1-run-b \
        --vs rerank-pool --vs-tags p-run-a p-run-b p-run-c

Free finding from building it: across the three existing `rerank` sweeps, **87 of
100 rows are unanimous**, so 13 rows carry the entire 7.1% flip rate.

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
- ~~**The graph path's statements carry no statute text.** A `ContextDoc` from this
  path holds the rendered statement and a citation label, by decision. On `both`
  the passage text arrives via the vector path; on `graph` — `ag-001` alone —
  Command A will see statements and no legislative prose. Whether that is enough
  to generate from is a Step 6 question and is untested here.~~ → **Answered by
  Step 6, and the interesting half is the route this bullet did not ask about
  (2026-08-05).** On `graph`, `ag-001` was served **50 statements and no
  legislative prose** and returned a complete answer with 50 citations and 4 gold
  chunks — so statements alone are enough to generate *something* citable.
  **n=1**, and it is the only `graph`-routed row in the eval set, so this is an
  existence result and not a rate. What Step 6 did find is that the premise of
  the *other* clause was wrong: on `both`, the phase plan's "dedupe by
  `chunk_id`" would have dropped the passage on **9 of the 10 routed rows**,
  removing the only statute text from the prompt precisely where this bullet
  assumed it would arrive safely. ADR-0014 records the reinterpretation. Whether
  a statement-only prompt is *good enough* remains open — that is answer
  accuracy, and it is Phase 5's judge, not this file's.
- **`path_between` is measured at zero.** It is the only cover for the 6
  untraversable relation types and the rules arm never fired it: S6 only triggers
  when nothing else matched, and something else always matched. Its provenance is
  also one arbitrary chunk per hop where parallel edges exist.
- ~~**Rerank leaves 10 of 13 available chunks unrecovered**, and nothing here
  explains why. The candidate pool is not the constraint — that is measured — so
  the next lever is chunking (ADR-0003) or a different ranking signal, not a
  bigger `top_k`.~~ → **Quantified at 100 rows (2026-08-16), and the bullet's own
  advice was then tested and failed.** It is 51 of 57 now, against 6 lost to the
  passage cap. "Not a bigger `top_k`" was right for the wrong reason: `top_k` was
  raised, in the strongest available form — a BM25 + full-text union that lifts
  the pool from 157/203 to 176/203, the largest retrieval gain measured on this
  corpus — and **+19 into the pool produced +2 into the prompt**, because
  ordering loss rose 51 → 66 as retrieval loss fell 46 → 27. The pool is not the
  constraint; feeding it more does not help while ranking is. The union is
  committed and off (`LEXICAL_DEPTH_LIVE = 0`).
- ~~**No per-stratum rerank claim is supportable at n=23.** Every stratum delta is
  0, +1 or +2 against a pre-registered resolution of ±2. The 100-row set is what
  would make two-hop's +1 either a finding or a nothing.~~ → **Answered at 100
  rows, and two-hop was the wrong stratum to watch (2026-08-16).** Two-hop is
  3 wins / 3 losses, dead even. The two claims that are now supportable point in
  opposite directions: **cross-regulation +5 with zero losses** at k=5 (same sign
  at k=8 and k=10), **aggregation −3**. The aggregate, meanwhile, went the other
  way — +4 at n=23 became +2 at n=100, which no longer clears the ±2 resolution,
  and the paired test puts rerank vs raw vector at p = 0.711.
- **Whether Rerank 3.5 should be deleted is genuinely open.** It nets to no
  measured difference at the shipping cap while consistently helping one stratum
  and consistently hurting another, for 9.4 s p95 and $0.0024/query. Deleting it
  recovers that and costs cross-regulation; keeping it pays for a stratum-level
  wash. Neither is obviously right, and the decision should be taken against
  whatever replaces the ranking stage rather than in isolation.
- **The ranking stage is the binding constraint on the vector path, and nothing
  measured so far moves it.** 51 of 203 gold references are in the pool, would fit
  under the cap, and are ranked below noise. A wider pool made it worse in
  absolute terms. The two candidate levers — a different reranker, or a chunking
  change — are both larger decisions than any taken here, and the 46/6/51 split
  is the evidence they should be argued against. **Structural reordering has now
  been eliminated as a third option** (2026-08-16): article diversity, graph
  connectivity and inbound `REFERENCES` were all measured offline against the
  committed pools, best result +2 chunks at p = 0.500, and all three fail for
  independent reasons. See §Structure as a reordering signal.
- **Six levers were measured and every one failed; the seventh worked by not
  playing.** Adding graph statements (0 wins / 4 losses), raising the passage cap
  (+6 chunks, ~80% noise), the lexical union (+19 pool → +2 prompt), article
  diversity (0 wins at any N), connectivity boosting (+2 at p = 0.500), inbound
  `REFERENCES` (+1, p = 1.000). All six tried to make the ranking stage better.
  **Enumeration moved aggregation gold in the prompt from 12/48 to 33/48 by
  removing the ranking stage from that stratum instead.** The generalisable
  lesson is not "enumerate more"; it is that the narrowing stage is beyond repair
  by tuning, and the wins available are in identifying question classes where it
  can be bypassed by something deterministic.
- **The relevance signal itself has still never been changed.** The cross-encoder
  is the one component in the narrowing stage no experiment has replaced, and it
  is what the 46/6/51 decomposition points at for the 80 rows enumeration does
  not touch. The next thing tried should be a different ranker, and if the eval
  set cannot resolve the difference it makes, that is a statement about the eval
  set.
- ~~**The aggregation stratum's constraint has moved from retrieval to generation,
  and nothing yet addresses the new one.** `ag-006` has all 4 gold paragraphs of
  Art. 99 in the prompt, cites all 4, and is `wrong` on all three runs because it
  pairs the wrong ceiling with the wrong infringement class.~~ → **Half right,
  and the wrong half was the diagnosis (2026-08-16).** `ag-006` was not a
  synthesis failure: its ranked top-5 carries **Article 100**, the fine schedule
  for Union institutions, and the EUR 1.5M / 750k figures in the wrong answer are
  copied from it. Dropping the ranked passages on enumerating rows
  (`rerank-enum-only`) fixes the row at **lower** cost than the arm it replaces.
  Decomposed synthesis was built for this row, did not turn out to be what fixed
  it, and is not adopted. Aggregation is now 4/10 against `rerank`'s 2/10.
- **`enum-only` beats `enum+top5`, which contradicts the augment-not-replace
  finding, and both are right.** Augmenting wins on RECALL -- it cannot lose gold
  the ranking already had, worth +4 gold chunks. Replacing wins on VERDICTS,
  because the ranked tail is also how Article 100 gets into `ag-006`. Recall and
  accuracy disagree here, and the accuracy measurement is the one that decides.
  Any future use of the retrieval numbers in this file should carry that caveat:
  gold-in-prompt is necessary and is not sufficient, and on this stratum more of
  it can be worse.
- **The judge has been caught grading in favour of an arm under test.** On
  `ag-008`, three decomposed answers omit the Art. 6(2) consequence that the
  grading rule calls partial -- none contains the string "high-risk" -- and all
  three were graded `correct`, while the identical omission under `enum-only` was
  graded partial 3 of 3. One reason states the disqualifying fact and passes the
  row anyway; another asserts the opposite of the text. The 85%/95% agreement
  figures were measured on `rerank`-shaped prose and have never been re-measured
  on a different ANSWER SHAPE. Decomposed answers are longer and
  citation-dense, which is the obvious candidate mechanism. Until that is
  re-measured, a comparison between arms that produce differently-shaped answers
  is weaker than one between arms that do not.
- **The end-to-end enumeration result is unresolvable at n=10** — one row is 10pp
  on that denominator, against a ±5.2pp noise band. It is 1 win / 0 losses vs
  `rerank` and 3 / 0 vs `vector`, which is encouraging and is not evidence.
  Resolving it needs more aggregation rows, and `eval-set.md` should treat that
  as a sampling requirement rather than a nice-to-have: the stratum is 10 rows
  and it is the one where the system's behaviour is now changing fastest.
- **The detector reaches 4 of 10 aggregation rows and stops there on purpose.**
  `ag-003`, `ag-005`, `ag-007` and `ag-009` are enumeration questions the regex
  does not match, and `ag-003` in particular fails for exactly the reason
  enumeration would fix. The widened variant that catches them costs 6.2% false
  positives on the other 80 rows. A better detector is the obvious next
  increment, and it should be a *classifier over question shape* measured on
  precision, not more regex alternatives.
- **`aia-art6-para2` is a recurring single point of failure and has never been
  looked at directly.** It is displaced from the top 5 on `th-015`, `3h-015`,
  `hn-008` and `3h-002`, outranked by `aia-art6-para3` on three of them, and it
  is 3 of the 4 chunks the inbound-`REFERENCES` probe recovers. One paragraph
  accounting for four rows across three strata is more likely a property of that
  chunk — its length, its embedding, its overlap with its own neighbour — than
  four independent ranking accidents. Cheap to inspect, never inspected.
- **The entity linker resolves an Article or Annex entity on only 15 of 90
  scoreable questions**, and 0 of 15 cross-regulation rows. Any method anchored
  on a named provision — inbound `REFERENCES`, an enumeration bypass, a
  locator lookup — inherits that ceiling before it does anything else. General
  entity coverage is 90 of 90 and is not the problem; *referential* coverage is.
  Whether that is the linker's `ANCHOR_TYPES` (see the bullet above) or simply
  what these questions are, is unmeasured.
- **The rerank rate has no first-party source.** See above. `search_units` is
  measured; the dollars are not.
- ~~**`AskResponse.cost_usd` is `float` and required (`src/schemas.py:294`).** It
  can now be filled for the vector route, but only because `RERANK_PRICE_PER_SEARCH`
  is a number rather than `None`. If that constant ever goes back to `None` — the
  honest state if the aggregator figure is withdrawn — the field cannot represent
  the route's cost. Step 7 has to decide between `float | None` and a
  "priced components only" flag; this is flagged now so it is not discovered then.~~
  → **CLOSED by Step 7 (2026-08-05): widened to `float | None`** at
  `src/schemas.py:366`. (This bullet's `:294` was already stale when it was
  written; the field had moved.) The flag option was rejected because it puts a
  number and its own trustworthiness in two fields, and a consumer that reads the
  first without the second under-reports silently — which is the failure
  `price_of`'s docstring forbids in the first place. `null` cannot be added up by
  accident. Today no live route reaches it: all 23 rows priced, 0 unpriced. The
  arm is held by a test that sets the constant back to `None` rather than by
  waiting for the day the rate is withdrawn.
- **Rerank latency is unmeasured on a production key.** 286 ms p50 comes from 20
  clean calls on a trial key whose tail behaviour is visible in the stalls above.
  **Step 7 partially re-ran this and it did not settle** (2026-08-05): the same
  two ids, `hn-001` and `sh-006`, are again the slowest end-to-end rows, at
  12.5 s and 24.8 s against 83.5 s and 82.4 s here. Same rows, a quarter of the
  margin — consistent with retry backoff and with a less contended key alike, at
  n=1 per row. See `answer-path.md`.
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
