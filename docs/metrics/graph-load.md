# Graph load metrics (Phase 1 Step 4)

Status: **loaded and idempotent.** 3,366 nodes and 6,658 relationships in Neo4j from 1,107 extracted
chunks. All six Cypher templates return rows; three had to be fixed to get there.

Companion to `extraction-cost-and-findings.md`, which measures what came *out of the model*. This
measures what came *out of the loader* — the graph's shape, the load's cost, and a row-count baseline
for the Phase 3 templates.

Regenerate everything here with:

```bash
python -m src.ingest.graph_writer                 # derivation only, no database needed
python -m src.ingest.graph_writer --apply --verify --report
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
| `write_edges()` — 6,658 rows, 13 statements | 0.97s | 0.72s |
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
| Relationships | **6,658** |
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
| INTERACTS_WITH | 130 | 12 |
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
| **Loaded** | **6,658** |

**The naive join loses half the graph**, which is the single most load-bearing fact about this stage.
`resolved-entities.json` holds nodes only, and edges in `extractions.jsonl` carry raw, pre-normalised
`head`/`tail` strings. The loader therefore imports the resolver instead of reading its output file.

**Type reconciliation repaired a third of the endpoint violations for free:** 357 before resolution,
**241 after** — 116 fixed (32%), because a violation caused by a name being typed inconsistently
across chunks stops being a violation once the name has one agreed type.

## Template baseline

Measured against the loaded graph. These are the Phase 3 regression anchors — a template that
silently stops matching should change a number here.

| Template | Parameter | Rows | Coverage |
|---|---|---|---|
| `obligations_for_role` | `deployer` | 60 | 52 of 78 ActorRole nodes return ≥1 row |
| `obligations_for_system` | `high risk ai system` | **169** | 41 SystemType nodes classified; 7 also reach obligations |
| `enforcement_chain` | an obligation with `ENFORCED_BY` | ≥1 | 216 obligations enforced; only **4** also `PENALIZED_UNDER` |
| `definition_of` | `provider` | 1 | **334** terms defined in an Article or Annex |
| `cross_regulation` | `aia art. 2(7)` | 4 | 146 nodes carry ≥1 `INTERACTS_WITH` |
| `path_between` | `deployer` ↔ `GDPR` | 1 path, 2 hops | — |

**`obligations_for_system` returned 24,428 rows before `RETURN DISTINCT`** — 169 × the 124 parallel
`CLASSIFIED_AS` edges (approximately; the multiplication is per matched path). Every node-projecting
template now uses `DISTINCT`, and the 169 is asserted exactly so that dropping it fails loudly.

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

`INTERACTS_WITH` endpoint types, for the record — no histogram in the extraction metrics doc records
endpoint *types*, which is precisely why the healthy edge count was mistaken for a healthy bridge:

| Head → Tail | Edges |
|---|---|
| Article → Regulation | 108 |
| Annex → Regulation | 11 |
| Regulation → Regulation | 10 |
| Right → Regulation | 1 |
| **Article → Article** | **0** |

## Load policies

Decided in writing, not by accident, and each one verifiable in the graph:

| Case | Count | Policy | Check |
|---|---|---|---|
| Endpoint violations | 241 | **Load, tag** `endpoint_violation: true` | `MATCH ()-[r]->() WHERE r.endpoint_violation RETURN count(r)` |
| Dangling edges | 107 | **Skip**, list in the report | `skipped_dangling_names` in the report JSON |
| Isolated nodes | 112 | **Load** | `MATCH (n:Entity) WHERE NOT (n)--() RETURN count(n)` |

The first and third follow this repo's existing rule that a dropped edge is indistinguishable from one
never extracted. The second is the deliberate exception: an undeclared endpoint has no type, therefore
no label, and an untyped node would give the unlabeled `path_between` shortest paths through nodes that
do not really exist.

## Open

- **No template projects a relationship.** `source_chunk_id` is on every edge and is the join key to
  pgvector, but every template returns nodes, so citation validation cannot yet say which paragraph
  asserted an edge it traversed. Phase 3 must fix this; it is not a one-line change.
- **`gdpr-art70-para1` has no edges at all** — the 864-token EDPB task-list paragraph never extracted,
  so "which authority does what" has no graph path. A chunking problem, recorded in the failure notes.
- **Alias-based merging is still unapplied** (ADR-0009), so `aliases` is carried as a node property but
  never used to collapse nodes. The Phase 3 entity linker is the intended consumer.
