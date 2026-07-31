# Knowledge Graph RAG for EU AI Regulation — Project Roadmap

**Project:** Hybrid Knowledge Graph + Vector RAG over the EU AI Act and GDPR, built entirely on the Cohere model stack.

**Why this project:** Almost every candidate has built vector RAG. Very few have built retrieval that answers questions requiring two or three hops across related entities — and almost nobody has done it over a regulated-industry corpus using the enterprise stack Cohere actually sells. This project targets FDE roles: it demonstrates retrieval architecture, evaluation discipline, cost awareness, and domain framing (sovereign AI / EU compliance) in one repository.

**Target build time:** 3–4 weeks part-time (10–12 h/week).

---

## 1. The one-paragraph pitch

Legal texts are graphs pretending to be documents. Article 26 of the AI Act imposes obligations on "deployers," a term defined in Article 3, for systems classified as high-risk under Article 6 via Annex III, enforced by authorities designated in Article 70, with penalties set in Article 99 — and Article 10's data governance rules explicitly interact with GDPR's lawful-basis requirements. A vector search can find any *one* of those passages. Only a system that models the relationships can answer *"A German SMB deploys an emotion-recognition system at the workplace — which obligations apply, who enforces them, and what fines are possible?"* This project builds both systems, benchmarks them against each other, and publishes the delta.

---

## 2. Requirements checklist

**Accounts and access**

- Cohere API key. Trial key for development (rate-limited, ~1,000 calls/month); production key before running the full extraction pass.
- Neo4j: Aura Free (cloud, ~200k node / 400k relationship cap — sufficient) **or** Docker locally. Recommendation: Docker for development, so everything runs from one `docker-compose.yml`.
- PostgreSQL 16 + pgvector: Docker locally. Optional stretch: Azure Database for PostgreSQL Flexible Server provisioned via Terraform (a strong extra signal given your IaC background — one small `/infra` folder in the repo).

**Local stack**

- Python 3.12, `uv` or `poetry` for dependency management.
- Key packages: `cohere` (SDK v5+), `neo4j`, `psycopg[binary]`, `pgvector`, `fastapi`, `uvicorn`, `pydantic` (v2), `httpx`, `beautifulsoup4` or `lxml` (EUR-Lex parsing), `tenacity` (retries), `rich` (CLI output).

**Data**

- Corpus from EUR-Lex (free, official, reusable with attribution):
  - **AI Act:** Regulation (EU) 2024/1689 — consolidated HTML/XML from EUR-Lex, CELEX `32024R1689`.
  - **GDPR:** Regulation (EU) 2016/679 — CELEX `32016R0679`.
  - Optional expansion later: Data Act, DSA, NIS2 (adds cross-regulation hops).
- Both regulations exist in official English **and** German versions — this enables the multilingual stretch goal.

**Discipline**

- A cost log from day one: every API call records model, input tokens, output tokens, computed cost.
- The eval question set is drafted in **week 1**, not week 4. You cannot design a system well without knowing the questions it must answer.

---

## 3. The Cohere model mapping

Every model call in the system is a Cohere model. This is deliberate: it turns the repo into a demonstration that you know their product surface end to end.

| Pipeline stage | Model | Why |
|---|---|---|
| Entity & relationship extraction | **Command A** (`command-a-03-2025`) | Strongest Cohere generative model; structured-output extraction needs reasoning quality. Command A+ (open-weights MoE, May 2026) is the alternative if you want the "latest flagship" story. |
| Query router (enum classifier) | **Command R7B** | Router only emits `graph` / `vector` / `both`. R7B is dramatically cheaper (~$0.0375/$0.15 per 1M tokens) and fast — the right tool for a high-volume classification call. Using the small model here *is itself a cost-engineering signal*. |
| Embeddings (chunks + entity resolution + question entity-linking) | **Embed v4** (`embed-v4.0`) | Multilingual (100+ languages — matters for German legal text), Matryoshka output dimensions (1536 → 512/256 truncation), int8 quantization support. |
| Reranking (vector path) | **Rerank 3.5** | Cohere's differentiator. The original guide doesn't include a rerank stage — adding it upgrades the benchmark from a 2-way to a 3-way comparison. |
| Grounded answer generation | **Command A** with the `documents` parameter | Cohere's Chat API returns **native citations**: spans of the answer mapped to source documents. What the guide asks you to prompt-engineer ("require a citation per claim, validate it") is a first-class API feature here. You still validate chunk IDs server-side. |

**Concept — why a reranker exists at all:** embedding search compares a query vector to document vectors that were computed *without seeing the query* (a bi-encoder). A reranker is a cross-encoder: it reads query and document *together*, which is far more accurate but too slow to run over millions of chunks. So the pattern is: cheap bi-encoder retrieves top-50, expensive cross-encoder reorders them, top-5 go to the LLM. Precision where it counts, speed where it doesn't.

---

## 4. High-level architecture

```
                    INGESTION (offline, run once + incremental)
┌──────────────────────────────────────────────────────────────────┐
│  EUR-Lex HTML/XML                                                │
│      │                                                           │
│      ▼                                                           │
│  Structure-aware parser  ──►  Chunker (article/paragraph level)  │
│      │                              │                            │
│      │              ┌───────────────┴───────────────┐            │
│      ▼              ▼                               ▼            │
│  chunk store   Command A extraction            Embed v4          │
│  (Postgres)    (entities + relations,          (1536-dim,        │
│                 Pydantic-validated)             int8)            │
│                     │                               │            │
│                     ▼                               ▼            │
│                Entity resolution              pgvector (HNSW)    │
│                (Embed v4 similarity)                             │
│                     │                                            │
│                     ▼                                            │
│                Neo4j (MERGE, edges carry chunk_id)               │
│                                                                  │
│  Shared key: chunk_id lives in BOTH stores. That is the trick.   │
└──────────────────────────────────────────────────────────────────┘

                    QUERY (online, FastAPI)
┌──────────────────────────────────────────────────────────────────┐
│  Question                                                        │
│      │                                                           │
│      ▼                                                           │
│  Command R7B router ──► enum: graph | vector | both              │
│      │                                                           │
│      ├─ graph:  extract entities → link to node IDs (Embed v4)   │
│      │          → parameterized Cypher template → paths          │
│      │          → path-to-prose converter                        │
│      │                                                           │
│      ├─ vector: Embed v4 query → HNSW top-50                     │
│      │          → Rerank 3.5 → top-5 passages                    │
│      │                                                           │
│      ▼                                                           │
│  Context assembly (dedupe by chunk_id, label graph vs. passage)  │
│      │                                                           │
│      ▼                                                           │
│  Command A chat + documents parameter → answer + citations       │
│      │                                                           │
│      ▼                                                           │
│  Citation validator (every cited chunk_id ∈ retrieved set,       │
│  else reject & regenerate once)                                  │
└──────────────────────────────────────────────────────────────────┘
```

**Concept — why hybrid instead of graph-only or vector-only:** embeddings capture *semantic similarity* ("what text sounds like this question?") but have no notion of *structure* — they cannot follow "obligation X applies to role Y which is defined in article Z." Graphs capture structure perfectly but are terrible at fuzzy language ("what does the law say about chatbots?" — the word "chatbot" never appears in the AI Act). Each covers the other's blind spot. The router's job is to know which blind spot the current question falls into.

---

## 5. Corpus and ontology (the week-1 decision)

### 5.1 Getting the corpus

EUR-Lex serves each regulation as consolidated HTML with stable element structure (articles, numbered paragraphs, points). Download the English consolidated versions of both regulations. Parse into a hierarchy: `Regulation → Chapter → Article → Paragraph → Point`. **Chunk at the paragraph level** (a full article is often too long; a point is often too short to stand alone). Each chunk gets a deterministic ID like `aia-art26-para1` — human-readable chunk IDs make debugging and citations dramatically easier than UUIDs.

### 5.2 Ontology — constrain it before writing any code

The guide's warning is correct: an open-ended "extract all entities" prompt produces a graph nobody can query. Fixed ontology, tuned to legal text:

**Entity types (8):**

| Type | Examples |
|---|---|
| `Regulation` | AI Act, GDPR |
| `Article` | AIA Art. 6, GDPR Art. 6 (note: numbers collide across regulations — namespace them) |
| `Annex` | Annex III (high-risk use cases) |
| `ActorRole` | provider, deployer, importer, distributor, controller, processor, data subject |
| `Obligation` | "maintain technical documentation", "conduct FRIA", "appoint DPO" |
| `RiskCategory` | prohibited, high-risk, limited-risk (transparency), minimal-risk |
| `SystemType` | emotion recognition, biometric categorisation, GPAI model, credit scoring |
| `Authority` | market surveillance authority, notified body, DPA, AI Office |

**Relationship types (10):**

`DEFINED_IN` (term → article) · `IMPOSES` (article → obligation) · `APPLIES_TO` (obligation → actor role) · `CLASSIFIED_AS` (system type → risk category) · `LISTED_IN` (system type → annex) · `REFERENCES` (article → article, the cross-reference backbone) · `ENFORCED_BY` (obligation → authority) · `PENALIZED_UNDER` (obligation → article) · `EXEMPT_FROM` (actor role/system type → obligation) · `INTERACTS_WITH` (article → article across regulations — the AI Act ↔ GDPR bridge, and your best demo material)

**Concept — why the ontology comes first:** the ontology *is* your query language. Every Cypher template in Phase 3 is written against these exact types. If the extractor is allowed to invent `RELATED_TO` and `MENTIONS` edges freely, no template can be written, and the graph degenerates into a worse version of vector search.

### 5.3 The eval question set (draft it now)

50–100 questions, stratified. Target distribution (**revised 2026-07-31** — 8 strata, total 100; the
refusal budget is unchanged at 10, split into two modes, and `hard-negative` is added with a target so
every stratum has one):

| Stratum | Count | Cites? | Example |
|---|---|---|---|
| Single-hop | 20 | yes | "How does the AI Act define a 'deployer'?" |
| Two-hop | 20 | yes | "Which transparency obligations apply to deployers of emotion recognition systems?" |
| Three-hop | 15 | yes | "A German company deploys a high-risk system from Annex III — which authority enforces the documentation obligations and under which article are violations fined?" |
| Cross-regulation | 15 | yes | "How do the AI Act's data governance requirements in Article 10 interact with GDPR lawful-basis requirements?" |
| Aggregation | 10 | yes | "List every obligation that applies to providers of GPAI models." |
| Out-of-scope (must refuse) | 5 | **no citation** | "What does the US AI Executive Order require?" — outside the corpus entirely; refuse and cite nothing. |
| Unanswerable (must refuse) | 5 | **no citation** | "How long must Art. 26(10) reports be retained?" — *in* the corpus, but the text states no period. Refuse by naming the specific absence. |
| Hard-negative (false premise) | 10 | **must cite** | "Under the GDPR, is an undertaking's fine the *lower* of the amount and the turnover percentage?" — reject the premise and ground the correction in retrieved text. |

**The three refusal modes are deliberately separate strata.** Out-of-scope and unanswerable both refuse
and must produce *no* citation; hard-negative refuses a premise but **must** cite the text that
corrects it. Averaging them into one "refusal" number hides which behaviour actually failed, and the
three carry different `must_cite` conventions that `tests/test_eval_questions.py` enforces per-mode.

Gold answers: write them yourself from the text (tedious, ~2 evenings, non-negotiable). Grading: exact-match is impossible for legal prose, so grade with a judge prompt (Command A, temperature 0) scoring `correct / partially correct / wrong / correct refusal` against the gold answer — and hand-verify a 20% sample so you can report judge agreement.

---

## 6. Low-level build plan by phase

### Phase 1 — Extraction into Neo4j (~week 1–2, the long phase)

1. **Parser:** EUR-Lex HTML → structured JSON (`regulation, chapter, article, paragraph, text`). Keep the raw text verbatim; citations must quote the real sentence.
2. **Extraction schema (Pydantic):**

```python
class Extraction(BaseModel):
    entities: list[Entity]        # type: Literal[...8 types], canonical_name, aliases
    relationships: list[Relation] # type: Literal[...10 types], head, tail,
                                  # source_chunk_id, confidence: float
```

   Call Command A per chunk with the ontology in the system prompt, few-shot examples, and instruction to return **only JSON**. Validate every response with Pydantic; on failure, retry once with the validation error appended to the prompt. Log the failure rate — it belongs in your README's honesty section.
3. **Entity resolution.** The hard part. Pipeline per new entity:
   - normalize (lowercase, strip punctuation, expand known abbreviations: "AIA" → "AI Act");
   - exact-match against existing canonical names → merge;
   - else Embed v4 both names, cosine similarity above a tuned threshold (start at 0.90, tune on ~30 hand-labeled pairs) → merge and append alias;
   - else create new node.
   Legal text is friendlier here than news text (regulations define their terms), but you will still hit "deployer" vs "deployers" vs "the deployer referred to in Article 26(1)".
4. **Idempotent writes:** Cypher `MERGE` on `(type, canonical_name)`, never `CREATE`. Every relationship carries `source_chunk_id` and `confidence` as properties. Re-running ingestion must be a no-op.
5. **Cost control:** before the full run, extract 10 chunks, measure avg tokens per call, multiply out. Ballpark: two regulations ≈ 1,200–1,500 paragraph chunks; at roughly 2–3k input + 500 output tokens per extraction call on Command A ($2.50/$10 per 1M), the full pass lands in the **$15–30 range** — cheap, but only because the corpus is small; state the math in the README anyway, because *showing you did the math* is the signal. Cache results keyed by `sha256(chunk_text)` so re-runs are free.

**Done when:** Neo4j Browser shows a connected graph; the query "which obligations apply to deployers" is answerable by eye; ingestion re-runs without duplicating nodes.

### Phase 2 — Vector index (~3–4 days)

1. Embed all chunks with Embed v4, `input_type="search_document"`, int8 embeddings, into a pgvector column with metadata: `chunk_id, regulation, article, paragraph, entity_ids[]`.
2. **Dimension experiment (small, publishable):** index at 1536 and at Matryoshka-truncated 512. Measure recall@10 on ~20 labeled queries. If 512 loses <2% recall, use it and note the 3× storage/speed saving — a free "I understand embedding economics" result.
   *Concept — Matryoshka embeddings:* trained so that the first N dimensions form a valid smaller embedding on their own; you truncate instead of retraining, trading a little recall for a lot of storage and latency.
3. HNSW index (`m=16, ef_construction=64` to start), tune `ef_search` on the labeled set. Do not proceed to Phase 3 with unmeasured retrieval.
   *Concept — HNSW:* a layered "skip-list for vectors": search starts on a sparse top layer, descends into denser layers, examining only a tiny fraction of all vectors. `ef_search` = how wide the candidate beam is: higher = better recall, slower.

### Phase 3 — Router + graph query path (~week 3, first half)

1. **Router:** Command R7B, few-shot prompt, returns one token from `{graph, vector, both}`. Below a confidence heuristic (or on `both`), run both paths and merge. Log every decision: `question, route, latency, outcome` — Phase 5 needs this and it cannot be reconstructed later.
2. **Question → node linking:** extract entity mentions from the question (R7B again, or reuse the Phase 1 extractor), then resolve to node IDs via alias lookup + Embed v4 similarity — the exact machinery from entity resolution, reused.
3. **Cypher template library — never let the model write raw Cypher.** ~6 parameterized templates keyed by query type:
   - `obligations_for_role(role)` — role ← obligations ← articles
   - `obligations_for_system(system_type)` — system → risk category → obligations chain
   - `enforcement_chain(obligation)` — obligation → authority + penalty article
   - `definition_of(term)` — term → defining article + text
   - `cross_regulation(article)` — INTERACTS_WITH neighborhood
   - `path_between(entity_a, entity_b)` — bounded shortest path (≤4 hops)
   The model chooses the template and fills parameters; the query itself is fixed. This is a security control (no injection into your DB), a reproducibility control, and an interview talking point.

### Phase 4 — Merged grounded answers (~week 3, second half)

1. **Path-to-prose:** convert graph paths into readable statements before they reach the prompt — `(deployer)-[APPLIES_TO]-(FRIA obligation)-[IMPOSED_BY]-(AIA Art. 27)` becomes *"Deployers of high-risk systems must conduct a fundamental rights impact assessment (AI Act, Article 27)."* Each statement keeps its `source_chunk_id`. Raw triples in the prompt produce awkward answers.
2. **Context assembly:** dedupe by chunk_id across both paths; pass everything to Command A via the `documents` parameter, with each document labeled `[GRAPH]` or `[PASSAGE]` in its metadata.
3. **Citation validation:** Cohere returns citation spans natively; server-side, assert every cited document ID was actually in the retrieved set. On failure (rare with the documents API, but non-zero), regenerate once, then fail loudly. Count these events — the rate goes in the README.

### Phase 5 — Benchmark and publish (~week 4)

1. Run **three** systems over the full eval set: (a) vector-only, (b) vector + Rerank 3.5, (c) full hybrid. Report accuracy broken out by stratum.
2. Expected shape — and the story of the whole repo: parity on single-hop, rerank closing some of the gap on two-hop, hybrid pulling decisively ahead on three-hop, cross-regulation, and aggregation. If the curve doesn't materialize, the debugging journey *is* the honest-failure-notes section.
3. Also report: latency p50/p95 per system, cost per query per system, one-time ingestion cost. Be explicit that the hybrid is slower and costlier per query — that honesty is what makes the accuracy claim credible.
4. README order: **benchmark table → 90-second demo video → architecture diagram → honest failure notes → setup.** Nobody scrolls; the table and video decide.

**Overall done-when:** a FastAPI `POST /ask` returns `{answer, citations[], route, latency_ms, cost_usd}` with validated citations, and the README opens with the three-way benchmark table.

---

## 7. Repository structure

```
kg-rag-eu-ai-act/
├── README.md               # benchmark table first
├── docker-compose.yml      # neo4j + postgres/pgvector
├── infra/                  # optional: Terraform for Azure Postgres (stretch)
├── src/
│   ├── ingest/             # parser, chunker, extractor, entity_resolution, graph_writer
│   ├── index/              # embedder, pgvector schema, recall harness
│   ├── query/              # router, entity_linker, cypher_templates, retriever, reranker
│   ├── answer/             # path_to_prose, context_assembly, generate, citation_validator
│   └── api/                # FastAPI app
├── eval/
│   ├── eval-questions.jsonl # stratified eval set + gold answers + gold chunk ids
│   ├── run_benchmark.py    # runs all three systems
│   └── judge.py            # LLM-judge + agreement check
├── data/                   # EUR-Lex sources + extraction cache (gitignored)
└── docs/                   # this roadmap, ADRs, RCA-style failure notes
```

Write 3–4 short ADRs (you already do this on Maestro): "Why hybrid over graph-only", "Why templates over generated Cypher", "Why paragraph-level chunking", "Embedding dimension choice". FDE reviewers love decision records more than code.

---

## 8. Resume line and interview framing

**Resume line (fill the numbers after Phase 5):**
> Built a hybrid knowledge-graph + vector RAG system over EU AI Act and GDPR (~N chunks, Neo4j + pgvector) on the Cohere stack (Command A, Embed v4, Rerank 3.5); raised multi-hop question accuracy from X% to Y% over a reranked vector baseline at Z ms p95, with natively grounded citations validated server-side.

**Interview angles this project hands you:**
- *"Where do embeddings fail?"* — you have a measured answer with a curve, not an opinion.
- *"How do you prevent hallucinated citations?"* — grounded generation + server-side validation + a measured rejection rate.
- *"How do you control LLM access to a database?"* — template library, parameterized only.
- *"Why Cohere?"* (in a Cohere loop) — you used every layer of their stack and can compare Embed v4 dimensions and R7B-vs-A routing economics from your own logs.
- Sovereign AI narrative: an EU-regulation compliance assistant is precisely the workload European enterprises want on sovereign infrastructure — connects directly to your confidential computing / digital sovereignty SME background.

## 9. The four failure modes (from the guide, localized)

1. **Open-ended ontology** → unqueryable graph. Mitigated: fixed 8/10 type sets, enforced by Pydantic `Literal` types.
2. **Skipped entity resolution** → "deployer" exists as four nodes and every multi-hop query silently fails. Mitigated: dedicated resolution stage with a tuned threshold and alias lists.
3. **Model-generated raw Cypher** → injection risk + unreproducible. Mitigated: template library.
4. **Corpus without relationships** → already solved: legal cross-references are the densest relationship structure available in free public text.

## 10. Stretch goals (only after Phase 5 ships)

- **Multilingual demo:** ingest the official German version, ask questions in German, retrieve across both — a one-day add showcasing Embed v4's multilingual strength and doubling the sovereign-AI story.
- Incremental ingestion (re-extract only changed chunks via the hash cache) instead of full re-runs.
- Community detection (Leiden) over the graph for corpus-level summaries ("summarize all deployer obligations").
- Graph-neighborhood visualization in the API response.
- Terraform-provisioned Azure deployment of the whole stack.

---

## 11. Week-by-week schedule (10–12 h/week)

| Week | Focus | Exit criterion |
|---|---|---|
| 1 | Corpus download + parser + chunker; ontology finalized; **eval questions drafted**; extraction schema + cost estimate on 10 chunks | Structured JSON of both regulations; cost math done |
| 2 | Full extraction run; entity resolution; Neo4j populated; embeddings + pgvector + recall measurement | Connected graph, measured recall@10 |
| 3 | Router, entity linking, Cypher templates; path-to-prose, grounded generation, citation validator | End-to-end `/ask` works on all routes |
| 4 | Three-way benchmark, judge + agreement check, README, demo video, failure notes | Repo public, video recorded |

Phase 1 always takes twice the estimate. That is normal; budget for it and do not skip entity resolution to catch up — it is the phase interviewers ask about.
