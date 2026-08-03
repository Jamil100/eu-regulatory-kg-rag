# Query path metrics

Status: **Entity linker and router built and measured. 23 of 23 eval questions
link to at least one graph node; 52% of links land in a gold chunk's entity set
(64% excluding instrument nodes). Routing is deterministic: 21 of 22 rows, $0.00,
3.5 ms — Command R7B scored 10 of 22, below the majority-class constant, and
never emitted `both` at all.** Phase 3 Steps 2–3 complete. Steps 4–7 append here.

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

## Open

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
- **Precision has no upper reference.** 52% (64% excluding instruments) is the
  first measurement of anything on this path; nothing says whether it is good.
  Step 5's template-selection accuracy against `ontology_edges` is the first
  number that will constrain it, because a wrong link there is a zero-row query
  rather than a slightly worse prompt.
- **The 23-row eval set is the limit on all of the above.** Coverage is 40 gold
  chunks over 1,108 — **3.6%** — and only 6 of 40 are GDPR-side
  (`eval-set.md`). Two strata are 2 rows each, so a single question moves them 50
  points.
