# Phase 3 + 4: from a populated store to a working `/ask`

**Status:** Steps 0–5 done (2026-08-02, 2026-08-03, 2026-08-04). **Next action is Step 6** — context
assembly, generation, citation validation. The graph path now runs end to end and its selector is
deterministic and adopted (ADR-0013): **24 of 32 gold chunks, the oracle exactly, at $0.00**. It
hands Step 6 a problem it did not have before — **2,886 statements over 9 questions, with nothing to
rank them by.** The router is deterministic and adopted (ADR-0012), so
`/ask` contributes $0.00 and ~3.5 ms before retrieval starts. The vector path is now measured end to
end: **$0.002 per question**, essentially all of it rerank, against $0.0000032 for the embedding —
so the per-query cost belongs to rerank and generate, not to embed.

**Scope.** Roadmap Phase 3 (router + graph query path) *and* Phase 4 (path-to-prose, grounded
generation, citation validation), which the roadmap puts in the same week under one exit criterion:
**"End-to-end `/ask` works on all routes."** The three-way benchmark is Phase 5 and is not in scope.

**Companion to `plan-to-populate-hybrid-store.md`**, which took the project from a 10-chunk pilot to
a populated hybrid store and is now closed. Every step below ends in a named artefact under `docs/`;
that is a requirement of the step, not a write-up afterwards.

---

## Context

The store is built and measured. What does not exist is anything that reads it.

- **Neo4j:** 3,366 nodes / 6,680 edges, idempotent load, 12 labels + a shared `:Entity`,
  13 relationship types (`docs/metrics/graph-load.md`).
- **pgvector:** 1,108 chunks at 512 and 1536 dims, `entity_ids` populated with 0 dangling references
  against the graph, 512 adopted (ADR-0004) on 3× storage and 8× latency
  (`docs/metrics/vector-index.md`).
- **`src/query/`:** only `cypher_templates.py` is implemented. `router.py`, `retriever.py`,
  `reranker.py`, `entity_linker.py` raise `NotImplementedError`.
- **`src/answer/`:** all four modules raise `NotImplementedError`.
- **`src/api/app.py:28`:** `/ask` raises. `/health` works.

**The number this phase exists to move.** Vector-only recall@10, measured before the graph path
existed and per stratum, not averaged:

| Stratum | micro recall@10 |
|---|---|
| hard-negative | **100%** |
| single-hop | **75%** |
| cross-regulation | 70% |
| three-hop | 67% |
| aggregation | **47%** |
| two-hop | **30%** |

**Two-hop 30% and aggregation 47% are the thesis under test.** ADR-0001 argued for a graph precisely
because embeddings have no notion of structure; this is that claim made falsifiable. Phase 5 will
report a delta against a measured baseline rather than an assumed one — but only if Phase 3 does not
quietly change what is being measured.

**The binding constraint (ADR-0002):** the model chooses a template and fills parameters. It never
writes Cypher. This is a security control, a reproducibility control, and the reason the six
templates exist as a fixed library.

**Carried in from the previous plan's *Deferred into Phase 3*:**

1. **No template projects a relationship** — Step 1, this phase's first job.
2. **The annex `section` defect** stays deferred by decision. `user_prompt()` (`extract.py:302-309`)
   never saw `section`, so Annex VIII/XI nodes are as ambiguous as the citation labels were before
   Step 5 fixed them. ~$0.70 / 32 sectioned chunks. **Flagged in output, not fixed here** — see
   Step 5 and *Deferred into Phase 5*.
3. **The eval set at 23 of 100** remains the parallel track. Non-blocking here; blocking at Phase 5.

---

## ~~Step 0 — Resolve three interface conflicts before writing any code~~ ✅ DONE

> **Outcome.** All three resolved. `ContextDoc` added to `src/schemas.py`; `retrieve`, `rerank`,
> `path_to_prose`, `assemble` widened to use it; `Chunk` byte-unchanged apart from the new sibling
> class. `PRICES` + `price_of()` added to `src/config.py`. ADR-0011 written. Suite **92 → 102 tests**
> (10 new: 4 on `ContextDoc`, 6 on `price_of`); **81 pass / 21 skip** with no containers running —
> the 10 new tests all pass, zero regressions.
>
> **The rerank price is `None`, not a number.** Cohere's public pricing page has moved Rerank 3.5 to
> an hourly "Model Vault" rate ($5/hr) with no visible per-search or per-token figure — the two
> numbers this repo already trusts (Command A $2.50/$10 per 1M, Embed v4 $0.12/1M) came from
> `extract.py` and `embedder.py`, and Command R7B's $0.0375/$0.15 per 1M is the roadmap's own
> citation, not independently re-priced. Guessing a rerank figure would have been indistinguishable
> from a measured one inside `cost_usd`. `price_of()` returns `None` and the plan requires it to
> propagate rather than default to zero — Step 4 must replace it with an actual measured bill.
>
> **Scope held at three.** `router.py` and `entity_linker.py` were read again while auditing for
> other `Chunk`-typed conflicts; neither has one (`route() -> Route` and `link() -> list[str]` return
> plain types), so they were left untouched, as the plan specified.

The stubs were written in Phase 0, before the graph, the index, or the corpus existed. Three of their
signatures cannot be honoured as declared. Settling them first is not tidiness: Steps 1–7 all consume
the answer, and discovering the conflict in Step 6 means rewriting Steps 1–5.

**1. `Chunk` cannot carry a retrieval score or graph provenance.**

`Chunk` is `model_config = ConfigDict(extra="forbid")` (`src/schemas.py:164`) with no score field and
no `source_chunk_id`. Yet:

- `retrieve(question, top_k=50) -> list[Chunk]` (`src/query/retriever.py:11`) — the scores that
  ordered the results have nowhere to live, and the reranker needs them.
- `path_to_prose(paths) -> list[Chunk]` (`src/answer/path_to_prose.py:16`) — a rendered graph
  statement is **not a corpus row**. Its docstring says "each statement keeps its `source_chunk_id`",
  a field `Chunk` does not have and must not gain.

Do **not** loosen `Chunk`. It is `extra="forbid"` for a reason recorded in its own docstring: the
previous version declared `article: str | None` against an int-writing chunker and rejected **1,000
of 1,108 rows**, with the 108 that "passed" doing so by silently discarding the four fields that
identified them.

Add a sibling model to `src/schemas.py`:

```python
class ContextDoc(BaseModel):
    chunk_id: str
    text: str
    citation_label: str
    source: Literal["GRAPH", "PASSAGE"]
    score: float | None = None
    derived: bool = False          # ADR-0010: the edge was inferred, not asserted
```

and widen `retrieve`, `rerank`, `path_to_prose`, and `assemble` to use it. `Chunk` keeps describing
the corpus; `ContextDoc` describes something on its way into a prompt. They are different things and
the stubs conflated them.

**2. No template projects a relationship.** Step 1.

**3. `AskResponse.cost_usd` is required and nothing can compute it.** `src/schemas.py:252` makes it
non-optional. The only price constant in the repo is `PRICE_INPUT_PER_TOKEN` at
`src/index/embedder.py:58`, and it is embed-only. Phase 3 needs a per-model input/output price table
and a per-request accumulator threaded through router → embed → rerank → generate. Put the table in
`src/config.py` next to the model names it prices, so a model swap and its price move together.

**Documentation deliverable**
- `docs/adr/adr-0011-context-document-model.md` — *why the context document is not a `Chunk`*.
  Record the 1,000-of-1,108 precedent as the reason `Chunk` was not widened.
- Note in the ADR which stub signatures changed, so the diff is not mistaken for scope creep.

---

## ~~Step 1 — Project relationships from the Cypher templates~~ ✅ DONE

> **Outcome.** All six templates project provenance and **no row count moved** — 60 / 169 / 1 / 1 / 4 /
> 1-path-2-hops, measured live before the rewrite and again after. Against this project's base rate of
> 3-of-6 broken on first contact, **0 of 6 broke**, because the graph was brought up and queried before
> anything was edited. `graph-load.md` §Open bullet 1 is closed. Suite **102 → 138 tests**; 36 new in
> `tests/test_graph_query.py`, of which **14 need no database**.
>
> **The defect was in the fix, not in the templates.** The natural provenance shape —
> `collect(DISTINCT {chunk: rel.source_chunk_id, ...})` — returns `[{chunk: null}]` rather than `[]` on
> a missed `OPTIONAL MATCH`, because `collect` drops nulls but a map literal is never null. That is a
> citation to nothing that passes every `if provenance:` check, and `enforcement_chain`'s optional leg
> is the common case (only **4 of 216** enforced obligations carry `PENALIZED_UNDER`), so it would have
> been the default. Found by probing the live graph before writing a template; OPTIONAL legs now
> collect the bare property and only `cross_regulation` (single mandatory leg) uses the map form.
>
> **The row count is now tested for its reason, not just its value.**
> `test_aggregating_provenance_is_what_holds_the_row_count` runs the naive projection beside the real
> template and asserts **24,428 and 169** in one test — the historical defect number, reproduced live.
>
> **Scope notes.** `derived` is surfaced on `cross_regulation` and `path_between` only, licensed by
> `test_derived_is_confined_to_interacts_with` asserting `{INTERACTS_WITH: 22}`. Execution lives in the
> new `src/query/graph_query.py` (`run_template`, `provenance_of`, `--baseline` CLI); `TEMPLATES` stays
> a `dict[str, str]` with a sibling `TEMPLATE_PARAMS`, so `tests/test_graph_writer.py` is untouched.
> Two limitations are documented rather than fixed: `path_between` provenance is one **arbitrary** chunk
> per hop where parallel edges exist (`allShortestPaths` is the 24,428 multiplication in another
> costume), and `obligations_for_system` returns the same **124** `classified_chunks` on all 169 rows —
> the hot fact is out of the row count but still in the prose unless Step 5 caps it.

## Step 1 — Project relationships from the Cypher templates (original)

The job `docs/metrics/graph-load.md` §Open names outright: *"No template projects a relationship…
Phase 3 must fix this; it is not a one-line change."* Until it is fixed, `source_chunk_id` — the
pgvector join key **and** the citation provenance — is unreachable from any query, so a graph-path
answer cannot be cited and cannot be validated.

**The trap, stated before writing the Cypher.** Edges are stored one per asserting chunk
(`graph_writer.py:337-345` merges on `source_chunk_id`), so a fact repeated across the corpus becomes
parallel edges. `high risk ai system -[:CLASSIFIED_AS]-> high risk` is asserted by **124 different
chunks**. Naively adding a relationship variable to `RETURN DISTINCT` re-introduces exactly the
multiplication `DISTINCT` was added to kill — `obligations_for_system` returned **24,428 rows where
169 were correct**.

The fix is to aggregate provenance per distinct node tuple rather than project edges as columns:

```cypher
MATCH (r:ActorRole {canonical_name: $role})<-[ap:APPLIES_TO]-(o:Obligation)<-[im:IMPOSES]-(a:Article)
RETURN r, o, a,
       collect(DISTINCT ap.source_chunk_id) AS applies_chunks,
       collect(DISTINCT im.source_chunk_id) AS imposes_chunks
```

Aggregation makes the non-aggregated columns the implicit grouping key, so `DISTINCT` is no longer
needed and the row count does not move. `collect` drops nulls, which is what the `OPTIONAL MATCH`
legs in `obligations_for_system` and `enforcement_chain` need. For `path_between`, provenance comes
off `relationships(p)`.

> **This shape is a design, not a measurement.** Neo4j was unreachable when this plan was written
> (`ServiceUnavailable` — the container lives in WSL2, which stops it when the last session closes),
> so the claim "aggregation leaves the row counts unchanged" is reasoning about Cypher semantics,
> not an observation. **The first action of Step 1 is to bring the graph up and check it**, before
> any template is rewritten. This project's own record on Cypher written against a graph nobody
> queried is three defects in six templates, and all three returned rows.

**Also surface `derived`.** ADR-0010 tagged the 22 promoted cross-regulation bridges `derived: true`
specifically so a consumer can tell an inferred edge from an asserted one. A cross-regulation answer
should be able to say which it used — that is the whole reason the flag exists, and Phase 3 is its
first consumer.

**Regression anchors — these row counts must not move** (`docs/metrics/graph-load.md` §Template
baseline; the 169 is already asserted at `tests/test_graph_writer.py:315`):

| Template | Parameter | Rows |
|---|---|---|
| `obligations_for_role` | `deployer` | 60 |
| `obligations_for_system` | `high risk ai system` | **169** |
| `enforcement_chain` | an obligation with `ENFORCED_BY` | ≥1 |
| `definition_of` | `provider` | 1 |
| `cross_regulation` | `aia art. 2(7)` | 4 |
| `path_between` | `deployer` ↔ `GDPR` | 1 path, 2 hops |

**There is no Neo4j driver factory in `src/query/`.** `graph_writer.connect()` (line 268) is the only
one and it lives on the ingest side. Add an execution helper that binds parameters and returns rows —
and keep it importable without a database, so the pure parts stay testable.

**Documentation deliverable**
- `docs/metrics/graph-load.md`: extend the §Template baseline table with a provenance column
  (chunks per row), and **close the first §Open bullet**.
- A failure-notes entry **if** a template breaks on contact. Three of six did in Step 4; that is the
  base rate this project has earned and the honest expectation to plan against.

---

## ~~Step 2 — Entity linker~~ ✅ DONE

> **Outcome.** `link()` and `link_detailed()` in `src/query/entity_linker.py`, deterministic, **0 API
> calls / $0.00 / 5.5 ms per question**. **23 of 23** questions link to ≥1 node; the **38** distinct
> names they reach all exist as live `:Entity` nodes, and every linked `ActorRole` returns rows from
> `obligations_for_role` — `deployer` at exactly **60**, the graph-load anchor, reached from
> `ag-001`'s wording rather than hand-typed. Suite **138 → 159 tests** (21 new in
> `tests/test_entity_linker.py`); **159 pass / 0 skip** with containers up. New
> `docs/metrics/query-path.md`; `graph-load.md` §Open alias bullet closed for lookup, still open for
> merging.
>
> **The embedding stage was not built,** per ADR-0009's own measurement (`dpo` vs `data protection
> officer` = 0.42, *below* legitimately-distinct pairs at 0.75). The bar it must now clear is
> concrete: beat 100% link rate and 52% precision at $0.00 and 5.5 ms. The stub docstring was
> corrected to describe what exists.
>
> **The metric this step was given was wrong, and measuring the denominator first is what caught
> it.** Gold-chunk `entity_ids` averages **18.9 nodes per row** (75 for `ag-001`) against 3.6 links
> per question, so recall against it is capped by arithmetic — reported as a labelled lower bound
> (10%) with precision as the headline (**52%**, or **64%** excluding `Regulation` nodes, which
> questions name and chunks rarely assert). All five zero-scoring rows are penalty questions whose
> gold sets are article-citation nodes plus strings like `administrative fine up to eur 20 000 000 or
> 4 % of total worldwide annual turnover` — text no question contains. `ag-003` links correctly and
> scores 0. That is the argument for the hand-labelled set this plan deferred.
>
> **Two defects, both of which returned plausible results.** `_trim` peels `" .,;:"` and not `?`, so
> `"...a notified body?"` linked the obligation `notify use of real time remote biometric
> identification system` instead of `notified body`; and `normalize()` deletes apostrophes, so
> `"the GDPR's highest fine tier"` became `gdprs` and `ag-003` reached no instrument at all. The fix
> for the second had a defect of the same family — on multi-token spans `"the controller's"` shadowed
> `"controller's representative"` — now restricted to single-token spans. Both are regression tests.
>
> **Scope notes.** The plural fold was rebuilt over alias surfaces as well as canonical names (18 →
> **124** merges) using the resolver's own `_plural_map`, measured before adopting: **0** extra merges
> swallow a node that owns its name, and `premises`/`analysis`/`business`/`bias`/`practices` stay
> unmerged. `LinkedEntity` lives in `entity_linker.py`, not `schemas.py`. Ambiguity policy is
> implemented but **untested by measurement** — 372 alias surfaces are ambiguous, yet **0 of 79**
> links come out ambiguous, so both report arms are identical; it needs the eval set at 100.

## Step 2 — Entity linker (original)

`link(question) -> list[str]` returning canonical node keys usable directly as Cypher parameters.
`schema.sql:50-53` already guarantees the shape: `entity_ids` are "the same string as the Neo4j MERGE
key, so a value here is usable as a Cypher parameter with no translation."

**Reuse, do not rebuild.** `normalize()` (`src/ingest/entity_resolution.py:87`) and the plural map
inside `resolve_corpus()` (line 278) are the Phase 1 machinery the roadmap says to reuse. Three traps
worth writing down before they are rediscovered:

- **`resolve_corpus()["key"]` is keyed on raw names that appeared in the corpus**, so a span from a
  user's question is usually *not* a key. `normalize()` plus the plural fold is the reusable path —
  and the fold is corpus-dependent (`_plural_map` merges only where both forms are attested), so it
  must come from the same `resolve_corpus()` call, not be reimplemented.
- **Canonical names are not all lowercase.** `ABBREVIATIONS` maps `gdpr` → `GDPR` (asserted at
  `tests/test_graph_writer.py:201`), and `tests/test_graph_writer.py:273` passes `"GDPR"` as a live
  template parameter. A linker that lowercases unconditionally silently loses every instrument node.
- **`normalize()` is bracket-aware on purpose** (`_trim`, lines 65-84). A naive `.strip("()")`
  reproduces the exact defect ADR-0009's Correction section documents, where **1,026 of 3,366 node
  keys read `aia art. 1(1`** — invisible to every merge test, because it was not a merge error.

**`aliases` finally gets a consumer.** It is carried on every node and used by nothing;
`docs/metrics/graph-load.md` §Open names the Phase 3 entity linker as its intended consumer. Alias
lookup belongs here — with ADR-0009's warning attached, since the same candidate list that offers 88
real merges also offers `law enforcement agency` ← `law enforcement purposes`.

**Ground truth, free.** Each eval row's `source_chunk_ids` → those chunks' `entity_ids` in pgvector
is the node set the question ought to reach. That gives linker precision/recall over the 21 scoreable
rows with **no new hand-labelling**, and it is a genuine cross-check rather than a restatement:
`entity_ids` came from the resolver, the gold chunks came from reading the law.

**Documentation deliverable**
- **New `docs/metrics/query-path.md`** — the fifth companion to `extraction-cost-and-findings.md`,
  `graph-load.md`, `eval-set.md`, and `vector-index.md`. Opens with a Status line, a "Regenerate
  with:" block, `## Shape`, and a mandatory `## Open`.
- Linker precision/recall per stratum, plus how many of the 23 questions link to ≥1 node at all. A
  question that links to nothing cannot take the graph route no matter what the router decides.

---

## ~~Step 3 — Router: Command R7B measured against a deterministic baseline~~ ✅ DONE

> **Outcome.** Rules adopted, **21 / 22** at **$0.00** and **3.5 ms** p50. Command R7B scored
> **10 / 22** — *below the majority-class constant* `always-vector` at **13 / 22** — and failed the
> pre-registered hard gate by routing `xr-004` to `graph`. ADR-0012 records the adoption and the
> loser's full numbers. Suite **159 → 197 tests** (4 in `test_eval_questions.py`, 34 in the new
> `tests/test_router.py`); **197 pass / 0 skip**. `failure-notes.md` Router misclassification rate
> filled at **4.5%**.
>
> **R7B returned `both` for 0 of 23 questions.** Not rarely — never: 15 `vector`, 8 `graph`, zero
> unparseable, zero errors. 9 of the 22 scored rows are gold `both`, so the missing class capped it at
> 13/22 before any judgement was made. Caught by printing the raw-output *distribution* beside the
> accuracy; 45% on a three-way task reads as a weak-but-working classifier and would have been written
> down as one. A second system prompt built specifically to attack the collapse — an ordered decision
> procedure testing `both` first and naming it the most common case — moved it **not at all** (`both`
> still 0 of 23, accuracy still 10 of 23, one answer newly unparseable). The rejected prompt is in
> ADR-0012 verbatim.
>
> **The plan's own "strong rule" fires on nothing.** *"A question that links to zero nodes has no graph
> path available"* is R1, and it fired **0 times** — Step 2 had already measured the link rate at 23 of
> 23 and written it into `query-path.md`, the file this step cites. The rule that actually carries
> those rows is **R2**: a question can link to five real nodes and still have nothing to traverse from,
> because `Regulation`, `DefinedTerm`, `Authority` and `Penalty` are not parameters any template
> declares. R1 is kept as a request-path guard with a test asserting its inertness, so a linker
> regression makes it load-bearing loudly.
>
> **The rules' 95% is in-sample and is labelled so everywhere it appears.** They were authored with all
> 23 gold labels visible; R7B saw none of them, and a test asserts no few-shot example is an eval
> question. What survives the asymmetry is the gate failure and the missing class, neither of which is
> an accuracy question. The single rules miss, `th-004`, is left unrepaired: the obvious fix moves
> `oos-002` the wrong way.
>
> **Scope notes.** `Route` moved to `src/schemas.py`, where `AskResponse` had been spelling the same
> three values a second time with nothing pinning them together. The sweep artifact is
> `eval/router-eval.jsonl` and not under `data/`, which `.gitignore` excludes wholesale — tests and the
> metrics doc read their numbers from it with no API key and no spend. The append-only decision log
> (`src/query/decision_log.py`) closes the shape of `failure-notes.md` §3 for one file. Two gold labels
> (`ag-001`, `ag-003`) override the mechanical seed and carry a `route_reason`; a test refuses a silent
> override.

## Step 3 — Router: Command R7B measured against a deterministic baseline (original)

The roadmap specifies R7B and makes the small-model choice a cost-engineering signal. This project
has also twice found the deterministic stage beat the model — ADR-0009 is an entire ADR about the
sophisticated stage being the one that did not work. **Build both, measure both, adopt on evidence,
and record which won.** If R7B wins, the cost story is intact and now earned. If rules win, that is a
better finding than the one the roadmap predicted.

**Gold route labels.** Add a hand-verified `route` field to the 23 eval rows. It is *derivable* from
`stratum`, `ontology_edges`, and `graph_traversable` — but derive-then-verify, never derive-and-trust:
`xr-003` and `xr-004` carry `graph_traversable: false` because AIA Art. 99 and GDPR Art. 83 never
cross-cite, so the honest label for them is `vector`, not `graph`, even though they are
cross-regulation. Extend `tests/test_eval_questions.py` (16 tests today) to enforce the new field the
way it already enforces `must_cite` per stratum.

**The two routers.**
- **R7B** — few-shot, one token out, `settings.model_router` (`command-r7b-12-2024`).
- **Deterministic baseline** — rules over question shape and the linked-entity count from Step 2. A
  question that links to zero nodes has no graph path available; that alone is a strong rule.

**The decision log is a requirement, not telemetry.** The roadmap: *"Log every decision: question,
route, latency, outcome — Phase 5 needs this and it cannot be reconstructed later."* Append-only
JSONL. Apply the `failures.jsonl` lesson (`docs/failure-notes.md:509`, still `OPEN`): **do not let
the next run destroy the last one's detail.** Getting this wrong is cheap now and unrecoverable in
Phase 5.

**Documentation deliverable**
- `docs/adr/adr-0012-router-model-vs-rules.md` — the measurement and the adoption, with the losing
  option's numbers kept in the ADR rather than deleted.
- Router accuracy and confusion matrix into `docs/metrics/query-path.md`.
- **Fill `Router misclassification rate: _TBD_`** at `docs/failure-notes.md:39`.

---

## ~~Step 4 — Vector path: retriever + reranker~~ ✅ DONE

> **Outcome.** `retrieve()` and `rerank()` built with request-path error handling, measured, and the
> `vector-index.md` reranker §Open bullet closed. Rerank 3.5 over the top-50 moves micro recall
> **23/51 → 27/51 at k=5** (+4 chunks) and **28/51 → 31/51 at k=10** (+3), hit rate@5 **76.2% →
> 85.7%**. Both clear the ±2-chunk resolution ADR-0004 declared for this eval set — **pre-registered
> before the first rerank ran**. Suite **197 → 258 tests** (61 new across `test_retriever.py` and
> `test_reranker.py`); **258 pass / 0 skip** with containers up. Sweep artifact `eval/rerank-eval.jsonl`
> stores the full retrieved *and* reranked 50 per question, so every published number is recomputed by
> a pure `scoreboard()` with no database, no API key and no spend.
>
> **This step's own instruction was the trap it warned about.** "Report recall@5-after-rerank against
> recall@10-before" charges a *perfect* reranker 9.8 percentage points, because micro recall is capped
> at `Σ min(gold_i, k)` — 45/51 at k=5 against 50/51 at k=10 — and the entire gap is one row
> (`ag-001` declares 11 gold chunks; every other question has ≤4). The comparison is same-k, with the
> ceiling printed beside every figure. Caught by computing the ceiling before running anything, which
> is the only reason it is a correction and not a published result.
>
> **The lever this step existed to pull did not move the number it was named for.** `vector-index.md`
> §Open called rerank "the obvious lever on the 30% two-hop figure". Two-hop went **3/10 → 4/10**:
> one chunk, half the pre-registered resolution. **No per-stratum delta clears ±2** — every cell is 0,
> +1 or +2 — so the aggregate gain is supportable and not one stratum-level claim is. A test asserts
> that, so the day one clears it, it has to be written down rather than absorbed.
>
> **The binding constraint is ranking, and that is now measured rather than assumed.** The top-50 pool
> holds **41 of 51** gold references against 31 returned at k=10, so **10 of the 13 chunks available to
> reordering are still unrecovered**. The per-query oracle `Σ min(|gold_i ∩ top50_i|, k)` was measured
> *before* `rerank()` was written, precisely so a null result could be attributed to the reranker
> rather than to an empty candidate pool. Two-hop has 7 of 10 in the pool: the right paragraphs are
> being retrieved and mis-ranked.
>
> **Two numbers were rejected for measuring the instrument.** Besides the k=5-vs-k=10 ceiling, a rerank
> **p95 of 83 s** turned out to be a Cohere trial key holding three single HTTP requests open (`hn-001`
> 83.5 s, `sh-006` 82.4 s, `th-003` 3.9 s) — all with `attempts=1`, so not retry backoff. `attempts` is
> now recorded per call and stalls are listed by name instead of averaged into a percentile that would
> be quoted as a model figure. p50 is **286 ms** over the 20 clean calls. Both cases are a new
> `failure-notes.md` recurrence row.
>
> **Scope notes.** The 512 per-stratum baseline was produced first from the *unmodified*
> `recall_harness` — the whole 29-vs-28 gap is cross-regulation and one chunk, so two-hop and
> aggregation are unaffected by the dimension choice. Pricing was split: the **quantity** is measured
> (`meta.billed_units.search_units`, 23.0 over 23 questions), the **rate** ($2.00/1K) is the only
> number in `config.py` with no first-party source — `cohere.com/pricing` publishes only a $5/hr Model
> Vault rate — so `price_of_rerank()` is a sibling of `price_of()` and `PRICES["rerank-v3.5"]` stays
> `None`, keeping `tests/test_config.py` green. `rerank-v3.5` was checked for deprecation and is
> current. `embedder.py` is **untouched**: the request-path embed calls `_embed_call` directly and
> raises `RetrieverError`, since catching `SystemExit` means catching `BaseException`. The reranker
> shipped without a retry and died on a 429 mid-sweep; it now carries the same tenacity policy as every
> other call site in the repo.

## Step 4 — Vector path: retriever + reranker (original)

- **`retrieve()`** — reuse the query shape proven in `src/index/recall_harness.py:62`, including the
  load-bearing `%s::vector` cast (psycopg adapts a bare list to `double precision[]` and `ORDER BY`
  has nothing to infer from). That function returns only `chunk_id`; widen the SELECT to build
  `ContextDoc`s, taking `citation_label` from the column rather than re-deriving it — it is `NOT NULL
  UNIQUE` in `schema.sql` precisely so the answer path never recomputes it.
- **Use 512 dimensions.** ADR-0004 is Accepted at 512. `embed_query()` (`src/index/embedder.py:155`)
  **defaults to 1536** — a trap that will silently query the wrong column or, worse, work while
  costing 8× the latency the ADR was decided on.
- **`rerank()`** — `rerank-v3.5` via `settings.model_rerank`. This is the only untried lever on the
  30% two-hop figure; `docs/metrics/vector-index.md` §Open scopes it to Phase 3 explicitly.
- **Re-measure recall per stratum, with and without rerank**, against the ADR-0004 baseline table.
  Reranking reorders top-50 into top-5, so report recall@5-after-rerank against recall@10-before
  honestly rather than picking whichever k flatters the delta.
- **`embed_texts()` converts a non-retryable `ApiError` into `SystemExit`** (`embedder.py:141`). That
  is correct for a CLI and wrong for a request path — a FastAPI worker should not exit because one
  question got a 400.

**Documentation deliverable**
- Rerank recall delta per stratum in `docs/metrics/query-path.md`, alongside the unreranked baseline.
- **Close the reranker §Open bullet** in `docs/metrics/vector-index.md`.

---

## ~~Step 5 — Graph path: template selection, execution, path-to-prose~~ ✅ DONE

> **Outcome.** The graph path runs end to end. Deterministic selection reaches **24 of 32 gold
> chunks — the oracle exactly** — at **$0.00** and **5.7 ms** p50, against Command R7B's **14 of 32**
> at $0.000159 and 951 ms; ADR-0013 records the adoption and keeps the loser's numbers. `ag-001`
> returns **11 of 11**, the aggregation row top-10 similarity cannot answer because it has 11 gold
> chunks. Suite **258 → 326 tests** (68 new across `test_template_selector.py` and
> `test_path_to_prose.py`); **326 pass / 0 skip** with containers up, **266 pass / 57 skip** with them
> down. New `src/query/template_selector.py`, `src/query/graph_path.py`, artifact
> `eval/selector-eval.jsonl`.
>
> **This step's own headline metric had a 90% floor, and measuring it first is the only reason that
> is a caveat and not a published result.** `ontology_edges` selection accuracy has a ceiling of
> **9 of 9** and a constant arm — `always-obligations_for_system`, which ignores the question — at
> **8 of 9**. Two templates traverse `APPLIES_TO`/`IMPOSES`/`CLASSIFIED_AS` between them, which nearly
> every row declares. Gold yield replaced it as the headline; edge-intersection is still published,
> always beside its constants.
>
> **R7B lost on parameter values, not template choice, which is a different finding from ADR-0012's.**
> It mostly picked correct templates and could not fill them: `system_type='high-risk AI system'` → **0
> rows** where `'high risk ai system'` → 169, `article='gdpr'` → **0** where `'GDPR'` → 29. **4 of its
> 9 calls matched no node, and every one passes `validate()`** — which checks parameter *names* while
> the graph matches parameter *values*. *Rows are not correctness*, one layer down: **validation is not
> correctness either.** R7B is also not reproducible at `temperature=0, seed=42` (16 then 14 over
> identical sweeps), so its figure is asserted as a bound.
>
> **The rules reach the oracle, so selection is not the binding constraint** — the template library is.
> The 8 unreached chunks sit behind `REFERENCES` and `LISTED_IN`, which **no typed template
> traverses** (6 of the ontology's 13 relation types are typed-unreachable). Same shape as Step 4's
> finding that ranking rather than retrieval bound the vector path.
>
> **Scope notes.** The hot fact is capped structurally: one statement per relationship *leg*, deduped,
> so 169 rows render **one** classification statement citing 3 provisions and saying `+121 more`
> (asserted against both synthetic and live rows). `path_to_prose` was widened to
> `(rows, template, *, labels, max_provenance)` — the stub could only dispatch by sniffing keys —
> recorded in ADR-0013. **The plan's claim that `RiskCategory` over-claims in `router.ANCHOR_TYPES`
> was wrong**: `definition_of`'s `(t:Entity)` head accepts it, and the real disagreement is
> one-directional — 6 types the router *excludes* that do fill a parameter. The router is left
> unchanged so ADR-0012's 21 of 22 is not silently re-measured. The `derived` flag works live but is
> **inert on this eval set** and is reported as inert. `path_between` fired **zero** times. **No
> `_TBD_` was filled**: the plan said `failure-notes.md:39-40`, but the three `_TBD_`s are at lines
> 66-68 and all belong to Step 6 and Phase 5 — a new measured-rate bullet was added instead.

## Step 5 — Graph path: template selection, execution, path-to-prose (original)

**Template selection.** From the linked entities (Step 2) and question shape. Under ADR-0002 the
model may only pick a name and fill parameters — the selector's output must be validated against
`TEMPLATES.keys()` before anything reaches the driver. `ontology_edges` on each eval row is the
ground truth for which template a question needs, so selection accuracy is measurable on the same 23
rows.

**`path_to_prose`.** Renders node tuples plus Step 1 provenance into `ContextDoc`s.

- **`display_name` for prose, `canonical_name` for keys.** ADR-0009's Correction added `display_name`
  for exactly this: the graph would otherwise cite `high risk` and `aia art. 1(1)` instead of
  `high-risk` and `AIA Art. 1(1)`.
- Each statement carries the `source_chunk_id` set from Step 1, which is what makes a graph-derived
  claim citable and what citation validation will check in Step 6.
- Statements built on a `derived: true` edge should say so.

**Flag the deferred annex defect, do not fix it.** Annex VIII/XI nodes are ambiguous because the
extractor never saw `section` — the three Annex VIII "point 1" chunks reached Command A with
identical metadata. Graph-path output touching those nodes carries the caveat; the item stays `OPEN`
at `docs/failure-notes.md:1020`. This is a decision, not an oversight: recorded here so nobody later
reads a confident Annex VIII citation as verified.

**Documentation deliverable**
- Graph-path section in `docs/metrics/query-path.md`: template-selection accuracy against
  `ontology_edges`, rows returned per route, and how many questions the graph path can answer at all.
- A failure-notes entry for whatever breaks on first contact with real questions.

---

## Step 6 — Context assembly, generation, citation validation

- **`assemble()`** — dedupe by `chunk_id` across both paths, label `[GRAPH]` / `[PASSAGE]`. The label
  strings are specified in prose across three files and **the key that holds them is defined
  nowhere**. Decide it here and write it down. Dedupe order matters: a chunk reached by both paths
  should keep the graph statement's provenance, not lose it to a passage hit.
- **`generate()`** — Command A with the `documents` parameter, returning native citation spans.
  `Citation` is `{chunk_id, start, end, text}` (`src/schemas.py:236`), where `start`/`end` index the
  answer text.
- **`validate()`** — every cited `chunk_id` ∈ retrieved set; on failure regenerate once, then fail
  loudly. The declared signature returns `bool` only, so the retry loop and the event counter have no
  home in it — decide whether they live in `app.py` or a small counter module, and say so.
- **Citations must be string-comparable with the eval set.** `Chunk.citation_label` was built to
  match `eval/eval-questions.jsonl`'s `citations` exactly (`AIA Art. 9(2)`, `AIA Annex VIII(A)(1)`).
  Phase 5 grading depends on that identity holding.

**Documentation deliverable**
- **Fill `Citation-validation rejection rate: _TBD_`** at `docs/failure-notes.md:40`. If the rate is
  0, say what would have to happen for it to be non-zero — the failure notes already contain two
  entries about a zero that looked like success.

---

## Step 7 — Wire `POST /ask` and cost accounting

Router → paths → assemble → generate → validate, returning all five `AskResponse` fields.

- **Connection lifecycle.** `app.py` has no lifespan hooks, no driver, no pool.
  `embedder.get_client()` builds a **new `cohere.ClientV2` per call**, and `graph_writer.connect()`
  calls `verify_connectivity()` every time. Per-request construction of all three is the obvious way
  to make a fast path slow.
- **`cost_usd`** from the Step 0 price table, accumulated across router, embed, rerank, and generate
  calls. Report it per route — the roadmap asks explicitly for the hybrid to be shown as slower and
  costlier per query, because *"that honesty is what makes the accuracy claim credible."*
- **`latency_ms`** measured around the whole handler, not the model call.

**Done when:** `/ask` returns validated citations on all three routes — `graph`, `vector`, and
`both` — against live Neo4j and pgvector.

**Documentation deliverable**
- `docs/metrics/query-path.md`: latency p50/p95 and cost per query, **broken out per route**.
- `README.md`: setup and a worked `/ask` example. Leave the benchmark table for Phase 5.

---

## Step 8 — Close-out

- Strike Steps 0–7, each with a `> **Outcome.**` note in the shape used by
  `plan-to-populate-hybrid-store.md`.
- Fill the verification table below.
- Confirm the **two Phase-3 `_TBD_`s** (`docs/failure-notes.md:39-40`) are filled. The other two
  (lines 41-42) are Phase 5's and must stay open.
- Write the phase's failure-notes entry and update the **Recurrence tracker** — including a new row
  if this phase invents a new way to be wrong, which on the current record it will.
- `pytest` green, with DB-backed tests skipping rather than reddening (`tests/conftest.py`).

---

## Parallel track — the eval question set (23 → 100)

Unchanged from the previous plan: **parallel, non-blocking for Phase 3, blocking at Phase 5.** The
strata are the measurement, so the benchmark cannot be run at 23.

Phase 3 adds one thing to each row: the hand-verified **`route`** gold label from Step 3. Writing it
while writing new questions is nearly free; retrofitting it across 100 rows later is not.

Coverage caveat worth restating: 40 distinct gold chunks over 1,108 — **3.6% of the corpus**, and
only 6 of 40 are GDPR-side (`docs/metrics/eval-set.md`).

---

## Deferred into Phase 5

Named so Phase 3 does not silently build on them.

1. **The annex `section` defect.** ~$0.70, 32 sectioned chunks, forced `--chunk-id` re-runs because
   `cache_key()` hashes only model + system prompt + chunk text — a `user_prompt()` change **does not
   rotate the cache**, so a plain `--all` replays stale rows and reports success.
2. **`gdpr-art70-para1` has no edges at all** — the 864-token EDPB task list that never extracted.
   "Which authority does what" has no graph path for it; the vector path is the only cover.
3. **The README benchmark table has 6 accuracy columns; §5.3 now defines 8 strata.** The single
   `Refusal` column contradicts the roadmap's own argument that averaging the three refusal modes
   "hides which behaviour actually failed." Reconcile before publishing.
4. **`eval/judge.py`'s signature omits `grading_rule`** — `judge(question, gold, answer)`, while every
   eval row carries a long decisive rule that encodes the partial/wrong distinction and the
   cited-to-a-*retrieved*-chunk requirement. The signature needs widening before Phase 5 grades
   anything.
5. **`embedding_1536` retained without a job**, pending an eval set large enough to resolve a 2%
   difference.

---

## Verification

| Step | How you know it worked |
|---|---|
| ~~0~~ ✅ | ~~`ContextDoc` exists in `src/schemas.py`; `Chunk` is byte-unchanged; the four widened signatures typecheck; ADR-0011 written; a price table exists next to the model names~~ — all confirmed; 10 new tests (`test_schemas.py`, `test_config.py`), suite 92 → 102, 81 pass / 21 skip |
| ~~1~~ ✅ | ~~All six templates return provenance **and** the six baseline row counts are unchanged — `obligations_for_system('high risk ai system')` still exactly **169**; `graph-load.md` §Open bullet 1 closed~~ — all confirmed live; 36 new tests (`test_graph_query.py`), suite 102 → 138, 0 skipped with containers up / 14 pass + 22 skip with them down |
| ~~2~~ ✅ | ~~Linker precision/recall measured against `source_chunk_ids`→`entity_ids` over 21 rows; `GDPR` (uppercase) links correctly; no key contains an unbalanced paren; `docs/metrics/query-path.md` created~~ — all confirmed; precision **52%** (64% excl. instruments) with recall reported as a labelled lower bound after the denominator was measured at 18.9 nodes/row; **38/38** linked names live in Neo4j; 21 new tests, suite 138 → **159**, 0 skipped with containers up |
| ~~3~~ ✅ | ~~Both routers measured on 23 gold-labelled rows; ADR-0012 records the adoption *and* the loser's numbers; decision log appends rather than overwrites; `_TBD_` at `failure-notes.md:39` filled~~ — all confirmed; rules **21/22** adopted over R7B **10/22**, which also lost to the majority-class constant (**13/22**) and failed the pre-registered gate; R7B emitted `both` **0 of 23** times under two prompts; 38 new tests, suite 159 → **197**, 0 skipped with containers up |
| ~~4~~ ✅ | ~~Recall per stratum re-measured with and without rerank against the ADR-0004 baseline; retrieval confirmed running at **512** dims; `vector-index.md` reranker §Open closed~~ — all confirmed; measured against a **512** baseline produced first from the unmodified harness, not against the published 1536 table; **+4 chunks at k=5 / +3 at k=10** clear the pre-registered ±2 resolution while **no per-stratum delta does**; the k=5 ceiling (45/51) corrected the step's own reporting instruction; 61 new tests, suite 197 → **258**, 0 skipped with containers up |
| ~~5~~ ✅ | ~~Template selection measured against `ontology_edges`; prose uses `display_name`; every graph statement carries ≥1 `source_chunk_id`; derived edges identifiable in output~~ — all confirmed, and the first clause was measured to be nearly uninformative (ceiling **9/9**, constant floor **8/9**) so gold yield became the headline: rules **24 of 32 = the oracle** vs R7B **14 of 32**, whose 4-of-9 zero-row calls all passed `validate()`; prose asserted free of lowercase keys; zero dangling chunk ids across both stores; derived edges identifiable live but **inert on the eval set** and reported as inert; 68 new tests, suite 258 → **326**, 0 skipped with containers up |
| 6 | Every cited `chunk_id` provably ∈ retrieved set; regenerate-once path exercised by a test, not by hope; `_TBD_` at `failure-notes.md:40` filled |
| 7 | `POST /ask` returns all five `AskResponse` fields on all three routes; `cost_usd` is non-zero and per-route; p50/p95 recorded |
| 8 | Steps struck with outcome notes; both Phase-3 `_TBD_`s filled and both Phase-5 ones untouched; failure-notes entry written; `pytest` green |

Run `pytest` after every step. The suite is **92 tests across 6 files** at the start of this phase
(71 pass with no containers running; 21 skip on Neo4j/Postgres by design). Phase 3 should add test
files for `src/query/` and `src/answer/`, which have **none** today.

**One standing rule, from this project's own record.** `docs/failure-notes.md` opens with it: *a
change is marked `DONE` only if code in this repo enforces it; doing something once by hand is not
`DONE`.* Three of six Cypher templates were wrong on first contact with a real graph, and every one
of them returned rows. Rows are not correctness.
