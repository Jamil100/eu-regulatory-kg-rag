# Graph load metrics (Phase 1 Step 4)

Status: **loaded and idempotent.** 3,366 nodes and 6,680 relationships in Neo4j from 1,107 extracted
chunks. All six Cypher templates return rows; three had to be fixed to get there. As of Phase 3 Step 1
they also return the `source_chunk_id` of every edge they traverse, so a graph-path answer is citable.

Companion to `extraction-cost-and-findings.md`, which measures what came *out of the model*. This
measures what came *out of the loader* — the graph's shape, the load's cost, and a row-count baseline
for the Phase 3 templates.

Regenerate everything here with:

```bash
python -m src.ingest.graph_writer                 # derivation only, no database needed
python -m src.ingest.graph_writer --apply --verify --report
python -m src.query.graph_query --baseline        # the Template baseline table
```

`--report` writes `data/processed/graph-load-report.json`, which is the machine-readable version of
the tables below plus the full list of dangling endpoints. Read the numbers off the tool.

---

## Environment

| | |
|---|---|
| Neo4j | **5.26.28 Community**, APOC core enabled |
| Runtime | Docker **inside WSL2 Ubuntu** (not Docker Desktop), compose run from a WSL shell |
| Client | Windows CPython 3.12.0, `neo4j` Python driver, Bolt via WSL2 localhost forwarding |
| Constraints / indexes | 12 uniqueness constraints (one per label) + 1 range index on `:Entity(canonical_name)`; 15 indexes total including the 12 constraint-backed ones and Neo4j's 2 default token lookups |

`IS NODE KEY` is Enterprise-only, so identity is enforced as per-label property uniqueness on
`canonical_name`. Type reconciliation guarantees exactly one type per resolved name, which is what
makes that safe.

**WSL2 caveat:** `bolt://localhost:7687` reaches the container from Windows with no `.env` change, but
WSL terminates the distro once the last session closes and takes the container with it — the first
symptom is a container that reports `Exited (255)` shortly after a successful `compose up`.

## Load cost

Median of 3 runs each, full CLI wall-clock including interpreter start:

| Run | Wall-clock |
|---|---|
| **Cold** — first load after the container restarts | **9.4s** |
| **Warm** — steady state | **2.7–3.1s** |

In-process breakdown, so the cold/warm gap is attributable rather than mysterious:

| Phase | Cold | Re-merge |
|---|---|---|
| imports (`src.ingest.graph_writer`) | 0.55s | — |
| `build_graph()` — pure derivation, parses 1,107 rows through Pydantic | 0.18s | — |
| `apply_schema()` — 12 constraints + 1 index, all `IF NOT EXISTS` | 0.06s | 0.06s |
| `write_nodes()` — 3,366 rows, 12 statements | 0.73s | 0.58s |
| `write_edges()` — 6,680 rows, 13 statements | 0.97s | 0.72s |
| **graph write total** | **1.70s** | **1.30s** |

**The work is ~2s; the cold run's extra ~6s is Neo4j-side** — JVM warmup and query-plan caching for
the 25 distinct statements, plus first-time constraint creation. Worth stating plainly because an
earlier note in the step plan quoted "~10 seconds" from a single cold measurement and read as though
that were the cost of loading. It is the cost of *starting*.

Batching is `UNWIND $rows` at 1,000 rows per `execute_write`, one static statement per label and per
relationship type — Neo4j 5 cannot parameterize a label or relationship type, so both are
interpolated after being checked against the closed `Literal` ontology.

## Graph shape

| | Count |
|---|---|
| Nodes | **3,366** |
| Relationships | **6,680** (6,658 extracted + 22 derived) |
| Distinct `(head, TYPE, tail)` triples | 6,311 |
| Parallel relationship instances | **347 (5.2%)** |
| Connected nodes | 3,254 |
| Isolated nodes | **112 (3.3%)** |
| Connected components | **31** |
| Largest component | **3,177** — 97.6% of connected nodes |

**Parallel edges are by design and they matter for querying.** One relationship is stored per
asserting chunk, because `source_chunk_id` is the citation provenance and folding it into a list would
make "which paragraph asserted this" unanswerable. The cost is that a fact repeated across the corpus
becomes parallel edges — `high risk ai system -[:CLASSIFIED_AS]-> high risk` is asserted by **124
separate chunks**. See the template baseline below for what that does to row counts.

### Nodes by label

| Label | Nodes |
|---|---|
| Obligation | 1,179 |
| Article | 1,169 |
| DefinedTerm | 484 |
| LawfulBasis | 132 |
| Authority | 86 |
| ActorRole | 78 |
| Regulation | 74 |
| SystemType | 69 |
| Right | 68 |
| Annex | 13 |
| Penalty | 11 |
| RiskCategory | 3 |

### Relationships by type

| Type | Edges | Endpoint-violating |
|---|---|---|
| APPLIES_TO | 2,581 | 75 |
| IMPOSES | 1,219 | 1 |
| REFERENCES | 1,161 | 62 |
| DEFINED_IN | 514 | 3 |
| ENFORCED_BY | 379 | 20 |
| LISTED_IN | 191 | 31 |
| CLASSIFIED_AS | 171 | 3 |
| PERMITS | 137 | 7 |
| INTERACTS_WITH | 152 (130 extracted + 22 derived) | 1 |
| GRANTS | 86 | 0 |
| EXEMPT_FROM | 58 | 27 |
| PENALIZED_UNDER | 19 | 0 |
| SETS_PENALTY | 12 | 0 |

## From extraction to graph

| Stage | Count |
|---|---|
| Relationships extracted | 6,767 |
| Endpoints resolvable via `resolve_corpus()["key"]` | **6,660 (98.4%)** |
| — same join by naive raw-string match against `resolved-entities.json` | **~48%** |
| Skipped as dangling (endpoint never declared as an entity) | **107**, 36 distinct names |
| Collapsed by `MERGE` on `(head, TYPE, tail, chunk)` | 2 |
| Derived cross-regulation bridges (see below) | **+22** |
| **Loaded** | **6,680** |

**The naive join loses half the graph**, which is the single most load-bearing fact about this stage.
`resolved-entities.json` holds nodes only, and edges in `extractions.jsonl` carry raw, pre-normalised
`head`/`tail` strings. The loader therefore imports the resolver instead of reading its output file.

**Type reconciliation repaired a third of the endpoint violations for free:** 357 before resolution,
**241 after** — 116 fixed (32%), because a violation caused by a name being typed inconsistently
across chunks stops being a violation once the name has one agreed type. Widening
`INTERACTS_WITH`'s allowed head to include `Annex` then cleared 11 more, leaving **230**.

## The derived cross-regulation bridge

`INTERACTS_WITH` had **zero `Article↔Article` edges**, and that was not the design: `ALLOWED_ENDPOINTS`
permits it and the roadmap specifies it outright — *"article → article across regulations — the AI Act
↔ GDPR bridge, and your best demo material."*

The extractor identified the foreign article correctly in **12 of 12** chunks that cite one, filed it
as `REFERENCES`, then emitted `INTERACTS_WITH` pointing at the *instrument*:

```
aia-art26-para9   REFERENCES      AIA Art. 26(9) → GDPR Art. 35   ← the bridge, present
                  INTERACTS_WITH  AIA Art. 26(9) → GDPR           ← collapsed to the instrument
```

Root cause is the Step 0 `RiskCategory` pattern again: **the system prompt's own few-shot example
teaches the collapse.** Fixing it at the source is an ontology-v4 change costing a ~$24 re-extraction;
the article-level link is already present under another type, so it is derived instead — deterministic
and free.

**The rule**, applied in `build_graph()`:

> for every `REFERENCES X→Y` where `X` and `Y` are both `Article`/`Annex` **and both carry a known
> instrument prefix** (`aia|gdpr|led|eudpr`) **and those prefixes differ**, emit `INTERACTS_WITH X→Y`
> with `derived: true`, inheriting `source_chunk_id` and `confidence`.

Result: **22 bridges — 19 `Article→Article`, 3 `Annex→Article`.**

**Requiring both ends to be namespaced is load-bearing, not defensive.** A first implementation checked
only the head, which treated a bare `article 35` as belonging to a different regulation and invented
**16 bridges no text supports**. The test asserts both endpoints carry a prefix.

**What the rule deliberately cannot do.** It fires only where a `REFERENCES` edge already crosses a
boundary. AIA Art. 99 and GDPR Art. 83 — the two penalty regimes — are conceptually parallel but
**neither cites the other**, so no bridge appears between them, and none is invented. Those eval
questions are marked `graph_traversable: false` instead. A test asserts no derived edge touches either
article.

| Endpoint shape | Before | After |
|---|---|---|
| Article → Regulation | 108 | 108 |
| **Article → Article** | **0** | **19** |
| Annex → Regulation | 11 | 11 |
| Regulation → Regulation | 10 | 10 |
| **Annex → Article** | **0** | **3** |
| Right → Regulation | 1 | 1 |

Two eval rows become genuine article-level traversals: `xr-001` gains
`AIA Art. 3(37) → GDPR Art. 9(1)` (the special-categories definition it asks about) and `xr-002` gains
`AIA Art. 26(9) → GDPR Art. 35` (the DPIA routing).

## Template baseline

Measured against the loaded graph. These are the Phase 3 regression anchors — a template that
silently stops matching should change a number here. Regenerate with:

```bash
python -m src.query.graph_query --baseline
```

**Provenance** is `source_chunk_id`s per row, min–max, as counted by `provenance_of()`. Every template
projects it as of Phase 3 Step 1; the row counts are unchanged from the pre-provenance measurement.

| Template | Parameter | Rows | Provenance / row | Coverage |
|---|---|---|---|---|
| `obligations_for_role` | `deployer` | 60 | 1–2 (48 distinct) | 52 of 78 ActorRole nodes return ≥1 row |
| `obligations_for_system` | `high risk ai system` | **169** | **124–126** (146 distinct) | 41 SystemType nodes classified; 7 also reach obligations |
| `enforcement_chain` | an obligation with `ENFORCED_BY` | ≥1 | 1–1 | 216 obligations enforced; only **4** also `PENALIZED_UNDER` |
| `definition_of` | `provider` | 1 | 1–1 | **334** terms defined in an Article or Annex |
| `cross_regulation` | `aia art. 2(7)` | 4 | 1–1 | 160 nodes carry ≥1 `INTERACTS_WITH` |
| `path_between` | `deployer` ↔ `GDPR` | 1 path, 2 hops | 2–2 (one per hop) | — |

**`obligations_for_system` returned 24,428 rows before `RETURN DISTINCT`** — 169 × the 124 parallel
`CLASSIFIED_AS` edges (approximately; the multiplication is per matched path). That number is now a
live assertion rather than history: `test_aggregating_provenance_is_what_holds_the_row_count` runs the
naive projection alongside the real template and asserts **24,428 and 169** in the same test, so the
reason the count holds is checked, not just the count.

**The 124–126 column is the same defect in its remaining form.** All 169 rows carry the same 124
`classified_chunks`, because 124 chunks really do assert that `high risk ai system` is `high risk`.
Aggregation moved the multiplication out of the row count, but a consumer that renders every chunk in
that list into a citation puts it straight back into the prose. Step 5's `path_to_prose` has to cap or
rank this; it is the one place the hot fact can still do damage.

**`path_between`'s provenance is one arbitrary chunk per hop.** `shortestPath` returns a single path,
and where a hop has parallel edges the driver picks one, so `chunks[i]` names *an* asserting chunk for
hop *i*, not *the* asserting chunk. `allShortestPaths` would enumerate them and is the 24,428-row
multiplication in another costume, so this is a limitation to state rather than fix. `chunks`, `types`
and `derived_flags` are positionally aligned with the hops, which a test asserts.

## Three template defects, and what each one teaches

All three are the same species: a query asserting a graph shape the corpus never produced. They differ
only in how loudly they failed.

| Template | Symptom | Cause | Caught by |
|---|---|---|---|
| `cross_regulation` | **0 rows** | Required `Article↔Article`; all 130 `INTERACTS_WITH` edges end at a `Regulation` | Simulating templates against resolved data before loading |
| `obligations_for_system` | **1,219 wrong rows** | Second `MATCH` unconnected — cartesian product | Same simulation; the row count was implausibly large |
| `definition_of` | **48 terms silently unreachable** | Tail pinned to `:Article`; `DEFINED_IN` also accepts `:Annex` | A row-count discrepancy between simulation (337) and live graph (286) |

The third is the most instructive, because it **passed a non-empty test**. The probe term, `provider`,
happens to be defined in an Article. A template can be 86% correct and look perfectly healthy, and the
only reason this surfaced is that two numbers that should have matched didn't, and the 51-term gap was
worth chasing rather than rounding off.

`INTERACTS_WITH` endpoint types **as extracted** — no histogram in the extraction metrics doc records
endpoint *types*, which is precisely why the healthy edge count was mistaken for a healthy bridge:

| Head → Tail | Edges |
|---|---|
| Article → Regulation | 108 |
| Annex → Regulation | 11 |
| Regulation → Regulation | 10 |
| Right → Regulation | 1 |
| **Article → Article** | **0** |

The zero on that last row is what the derived pass above fixes — and note that it was **not** visible
from the edge *count*, which stood at a healthy-looking 130. The count was never the risk; the endpoint
types were, and nothing was measuring them.

## Load policies

Decided in writing, not by accident, and each one verifiable in the graph:

| Case | Count | Policy | Check |
|---|---|---|---|
| Endpoint violations | 230 | **Load, tag** `endpoint_violation: true` | `MATCH ()-[r]->() WHERE r.endpoint_violation RETURN count(r)` |
| Dangling edges | 107 | **Skip**, list in the report | `skipped_dangling_names` in the report JSON |
| Isolated nodes | 112 | **Load** | `MATCH (n:Entity) WHERE NOT (n)--() RETURN count(n)` |

The first and third follow this repo's existing rule that a dropped edge is indistinguishable from one
never extracted. The second is the deliberate exception: an undeclared endpoint has no type, therefore
no label, and an untyped node would give the unlabeled `path_between` shortest paths through nodes that
do not really exist.

## Open

- ~~**No template projects a relationship.**~~ **Closed 2026-08-03** (Phase 3 Step 1). Every template
  now returns per-edge `source_chunk_id` aggregated inside `collect()` per distinct node tuple, and all
  six row counts are unchanged. `derived` is surfaced on `cross_regulation` and `path_between` — the
  only two that can traverse an ADR-0010 bridge, which a test enforces rather than assumes. Execution
  lives in `src/query/graph_query.py`; 36 tests in `tests/test_graph_query.py`, of which 14 need no
  database.
- **`gdpr-art70-para1` has no edges at all** — the 864-token EDPB task-list paragraph never extracted,
  so "which authority does what" has no graph path. A chunking problem, recorded in the failure notes.
- **Alias-based merging is still unapplied** (ADR-0009), so `aliases` is carried as a node property but
  never used to collapse nodes. The Phase 3 entity linker is the intended consumer.
