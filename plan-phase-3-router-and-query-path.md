# Phase 3 + 4: from a populated store to a working `/ask`

**Status:** Steps 0–1 done (2026-08-02, 2026-08-03). **Next action is Step 2** — the entity linker,
which turns a question into canonical node keys the Step 1 templates can be parameterised with.

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

## Step 2 — Entity linker

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

## Step 3 — Router: Command R7B measured against a deterministic baseline

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

## Step 4 — Vector path: retriever + reranker

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

## Step 5 — Graph path: template selection, execution, path-to-prose

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
| 2 | Linker precision/recall measured against `source_chunk_ids`→`entity_ids` over 21 rows; `GDPR` (uppercase) links correctly; no key contains an unbalanced paren; `docs/metrics/query-path.md` created |
| 3 | Both routers measured on 23 gold-labelled rows; ADR-0012 records the adoption *and* the loser's numbers; decision log appends rather than overwrites; `_TBD_` at `failure-notes.md:39` filled |
| 4 | Recall per stratum re-measured with and without rerank against the ADR-0004 baseline; retrieval confirmed running at **512** dims; `vector-index.md` reranker §Open closed |
| 5 | Template selection measured against `ontology_edges`; prose uses `display_name`; every graph statement carries ≥1 `source_chunk_id`; derived edges identifiable in output |
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
