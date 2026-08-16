 Diagnosis: why the hybrid does not beat vector                                                           │
│                                                                                                          │
│ Context                                                                                                  │
│                                                                                                          │
│ Four configs were benchmarked on a 100-row stratified set and the whole spread                           │
│ sits inside one standard error. Oracle routing scores the same as learned                                │
│ routing, so route selection has no remaining headroom. The question this session                         │
│ had to answer is whether the graph is misconfigured or whether it adds nothing on                        │
│ this corpus.                                                                                             │
│                                                                                                          │
│ It is not a routing problem and it is not primarily a retrieval-overlap                                  │
│ problem. The single decisive number is this:                                                             │
│                                                                                                          │
│ ▎ On all 28 rows where the graph actually fired, the graph produced                                      │
│ ▎ zero gains and four losses against vector-only. Net −4. There is not                                   │
│ ▎ one question on this eval set that the graph turned from a failure into a                              │
│ ▎ success.                                                                                               │
│                                                                                                          │
│ Everything below is recomputed from committed artifacts                                                  │
│ (eval/benchmark.jsonl, eval/answer-eval.jsonl, eval/selector-eval.jsonl,                                 │
│ eval/rerank-eval.jsonl, data/processed/). No code was run, no e             │

5. Citations are generator-attributed, not the retrieved set — vector cites 2.05 chunks against gold's 2.03. The inflation is a graph provenance fan-out at generate.py:369: on ag-001, 31 spans → 51 rows → 29 chunks, 25 of them GRAPH-sourced.

6. The harness already computes the right denominator (common_pass, ns that the per-system column isn't comparable — the README tablejust publishes the wrong one. Drops are logged. The exclusions reproduce your 97/94/96/93 exactly, and 16 of 20 excluded rows are wrong/partial — so the drift
inflates accuracy. Judge is fine: 85% exact, 95% binary agreement wi

Two flags where a fix improves the metric without improving the systominator lowers every number by ~1pp, and collapsing the citationfan-out fixes precision by counting less, not grounding better.

The deliverable is a diagnosis rather than an implementation plan, bnd three concrete experiments, so I'll hand it over for review.
User rejected Claude's plan:
╭──────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ Diagnosis: why the hybrid does not beat vector                              │
│                                                                                                          │
│ Context                                                                                                  │
│                                                                                                          │
│ Four configs were benchmarked on a 100-row stratified set and t             │
│ sits inside one standard error. Oracle routing scores the same as learned                                │
│ routing, so route selection has no remaining headroom. The ques             │
│ had to answer is whether the graph is misconfigured or whether it adds nothing on                        │
│ this corpus.                                                                │
│                                                                                                          │
│ It is not a routing problem and it is not primarily a retrieval             │
│ problem. The single decisive number is this:                                                             │
│                                                                                                          │
│ ▎ On all 28 rows where the graph actually fired, the graph prod             │
│ ▎ zero gains and four losses against vector-only. Net −4. There is not                                   │
│ ▎ one question on this eval set that the graph turned from a failure into a                              │
│ ▎ success.                                                                                               │
│                                                                             │
│ Everything below is recomputed from committed artifacts                                                  │
│ (eval/benchmark.jsonl, eval/answer-eval.jsonl, eval/selector-ev             │
│ eval/rerank-eval.jsonl, data/processed/). No code was run, no eval executed,                             │
│ nothing modified.                                                           │
│                                                                                                          │
│ ---                                                                         │
│ 1. Retrieval overlap — the hypothesis is refuted, and the truth is worse                                 │
│                                                                                                          │
│ The two routes do not return the same chunks. Measured over the             │
│ both artifacts, graph provenance vs vector top-50:                                                       │
│                                                                                                          │
│ ┌──────────────────────────────────────────────────────────────────┬───────────────────────────────────┐ │
│ │                             measure                                     │ │
│ ├──────────────────────────────────────────────────────────────────┼───────────────────────────────────┤ │
│ │ Jaccard(graph provenance, vector top-50)                                │ │
│ ├──────────────────────────────────────────────────────────────────┼───────────────────────────────────┤ │
│ │ graph provenance set size                                     243       │ │
│ ├──────────────────────────────────────────────────────────────────┼───────────────────────────────────┤ │
│ │ overlap after the adopted budget (answer-eval.overlap_chunk_in 1.6 of 5 │ │
│ └──────────────────────────────────────────────────────────────────┴───────────────────────────────────┘ │
│                                                                                                          │
│ So the routes are near-disjoint, not redundant. But the graph r             │
│ weaker, not complementary:                                                                               │
│                                                                                                          │
│ ┌───────────────────────────────┬─────────────┐                                                          │
│ │ gold recall (203 gold chunks) │   reached   │                                                          │
│ ├───────────────────────────────┼─────────────┤                             │
│ │ vector top-50                 │ 157 (77.3%) │                                                          │
│ ├───────────────────────────────┼─────────────┤                             │
│ │ rerank top-5                  │ 100 (49.3%) │                                                          │
│ ├───────────────────────────────┼─────────────┤                             │
│ │ graph provenance, uncapped    │ 73 (36.0%)  │                                                          │
│ └───────────────────────────────┴─────────────┘                                                          │
│                                                                             │
│ The graph's genuinely unique contribution — gold reached by graph and missed by                          │
│ vector top-50 — is 13 chunks across 9 questions (6.4% of gold), and it is                                │
│ concentrated exactly where you predicted: ag-001, ag-005 (aggregation),                                  │
│ xr-001, xr-006, xr-010, xr-013 (cross-regulation). The converse is                                       │
│ 97 chunks across 59 questions.                                              │
│                                                                                                          │
│ Why oracle routing changes nothing is now clear, and it is not              │
│ src/answer/context_assembly.py:145 sets ADOPTED_BUDGET = "first" and                                     │
│ :125 sets DEFAULT_BUDGET_N = 50. Every graph-routed row hits th             │
│ graph_available is 642 / 641 / 424 / 316 on the multi-hop rows and the budget                            │
│ takes the first 50 in template execution order. That is an arbi             │
│ Post-budget overlap with the vector passages collapses to zero, and the useful                           │
│ provenance goes with it.                                                                                 │
│                                                                             │
│ The proof that the material is there and the budget is what discards it —                                │
│ same question, same graph, different budget arm (eval/answer-eval.jsonl):                                │
│                                                                             │
│ same question, same graph, different budget arm (eval/answer-eval.jsonl):                                │
│                                                                                                          │
│ ┌─────────────────┬───────────────┬─────────────────────────┐               │
│ │       arm       │  gold_cited   │ GRAPH-sourced citations │                                            │
│ ├─────────────────┼───────────────┼─────────────────────────┤                                            │
│ │ uncapped        │ 35/51 (68.6%) │ 60.8%                   │               │
│ ├─────────────────┼───────────────┼─────────────────────────┤                                            │
│ │ rerank          │ 29/51 (56.9%) │ 31.3%                   │                                            │
│ ├─────────────────┼───────────────┼─────────────────────────┤               │
│ │ first (adopted) │ 28/51 (54.9%) │ 35.2%                   │                                            │
│ ├─────────────────┼───────────────┼─────────────────────────┤               │
│ │ roundrobin      │ 23/51 (45.1%) │ 35.1%                   │                                            │
│ └─────────────────┴───────────────┴─────────────────────────┘                                            │
│                                                                             │
│ And on the aggregation item specifically, ag-001:                                                        │
│                                                                                                          │
│ - uncapped → cited 10 of 11 gold Art. 26 paragraphs, finish_rea             │
│ - first (adopted) → cited 4 of 11                                                                        │
│                                                                                                          │
│ ADR-0014 adopted first on a 23-row sample because uncapped trun             │
│ th-001. The arms trade off in opposite directions per stratum, and a single                              │
│ global constant was fitted to 23 rows.                                                                   │
│                                                                             │
│ Verdict on Q1: the routes are meaningfully different. The graph is not dead                              │
│ weight because it duplicates vector; it is dead weight because              │
│ lower-recall, near-disjoint set and then throws away the best of it at the                               │
│ budget.                                                                     │
│                                                                                                          │
│ Also: the graph route is actively harmful when it fires                                                  │
│                                                                             │
│ ┌─────────────────────────────────┬───────────────────────┐                                              │
│ │                                 │                       │                                              │
│ ├─────────────────────────────────┼───────────────────────┤                                              │
│ │ hybrid rows with graph_sent > 0 │ 4/28 correct (14.3%)  │                                              │
│ ├─────────────────────────────────┼───────────────────────┤                 │
│ │ hybrid rows with graph_sent = 0 │ 31/72 correct (43.1%) │                                              │
│ ├─────────────────────────────────┼───────────────────────┤                 │
│ │ oracle rows with graph_sent > 0 │ 9/49 correct (18.4%)  │                                              │
│ ├─────────────────────────────────┼───────────────────────┤                 │
│ │ oracle rows with graph_sent = 0 │ 26/51 correct (51.0%) │                                              │
│ └─────────────────────────────────┴───────────────────────┘                 │
│                                                                                                          │
│ Paired, on the same questions, vector-only beats hybrid 8 vs 4 on the rows                               │
│ hybrid routed to graph, and 12 vs 9 on the rows oracle routed to graph. The                              │
│ four losses are th-010, th-011, th-015, th-016 — all two-hop, a             │
│ graph_sent=50.                                                                                           │
│                                                                             │
│ th-011 is the clearest case. Vector-only answers it correctly. Hybrid, given                             │
│ the same 5 passages plus 50 graph statements, replies "I'm sorr             │
│ find the answer to your question in the documents provided." with zero                                   │
│ citations. Injecting 50 graph statements caused a refusal on a question the                              │
│ system had already solved.                                                  │
│                                                                                                          │
│ ---                                                                                                      │
│ 2. Citation edge directionality — the edges exist, the traversal does not                                │
│                                                                                                          │
│ Are cross-reference edges extracted? Yes. REFERENCES is declare             │
│ src/schemas.py:64, endpoints (_PROVISION, _PROVISION) at src/schemas.py:106,                             │
│ and the extractor is instructed to emit one per citation at                 │
│ src/ingest/extract.py:224.                                                                               │
│                                                                             │
│ Are they stored with direction? Yes, and they are loaded:                                                │
│ data/processed/graph-load-report.json reports 1,161 REFERENCES              │
│ database — the third-largest edge type after APPLIES_TO (2,581) and IMPOSES                              │
│ (1,219). Direction is preserved (head → tail).                                                           │
│                                                                             │
│ Can traversal run inbound? There is no traversal at all. The entire query                                │
│ surface is six fixed templates in src/query/cypher_templates.py:43-138, executed                         │
│ through run_template at src/query/graph_query.py:120-133, which             │
│ name against TEMPLATES — the model cannot write Cypher (ADR-0002). None of the                           │
│ six touches REFERENCES. The code says so itself at                                                       │
│ src/query/template_selector.py:98-101:                                      │
│                                                                                                          │
│ ▎ path_between is [*..4] and untyped, so it traverses anything;             │
│ ▎ here means "no typed claim", and it is the only cover this library has for                             │
│ ▎ REFERENCES, LISTED_IN, SETS_PENALTY, EXEMPT_FROM, PERMITS and             │
│ ▎ the ontology's 13 relation types.                                                                      │
│                                                                                                          │
│ So 6 of 13 edge types, including the second-largest, are reacha             │
│ undirected 4-hop shortest-path query that requires you to already name both                              │
│ endpoints — i.e. to already know the answer.                                                             │
│                                                                                                          │
│ The data contains both answers you flagged. From                            │
│ data/processed/extractions.jsonl:                                                                        │
│                                                                             │
│ AIA Art. 99(4)  -REFERENCES->  AIA Art. 16     (chunk aia-art99-para4)                                   │
│ GDPR Art. 83(5) -REFERENCES->  GDPR Art. 12    (chunk gdpr-art8             │
│ GDPR Art. 83(5) -REFERENCES->  GDPR Art. 22    (chunk gdpr-art83-para5)                                  │
│                                                                             │
│ A single inbound template — MATCH (p)-[:REFERENCES]->(:Article {canonical_name:$article}) RETURN p       │
│ — returns aia-art99-para4 for th-006. That is exactly the missing gold chunk.                            │
│                                                                             │
│ Two corrections to the premise                                                                           │
│                                                                                                          │
│ th-006 is a genuine reverse-lookup gap; th-011 is not.                      │
│                                                                                                          │
│ ┌───────────────────┬─────────────────────────────────────┬────────────────────────────────────────┐     │
│ │                   │ th-006 (AIA Art. 16 → Art. 99 tier) │ th- limb) │     │
│ ├───────────────────┼─────────────────────────────────────┼────────────────────────────────────────┤     │
│ │ gold              │ aia-art16-para1, aia-art99-para4    │ gdpa5     │     │
│ ├───────────────────┼─────────────────────────────────────┼────────────────────────────────────────┤     │
│ │ in vector top-50? │ art16 yes, art99-para4 NO           │ bot       │     │
│ ├───────────────────┼─────────────────────────────────────┼────────────────────────────────────────┤     │
│ │ vector verdict    │ wrong (refused)                     │ cor       │     │
│ ├───────────────────┼─────────────────────────────────────┼────────────────────────────────────────┤     │
│ │ rerank verdict    │ wrong (refused)                     │ correct                                │     │
│ ├───────────────────┼─────────────────────────────────────┼───────────┤     │
│ │ hybrid verdict    │ wrong (refused)                     │ wrong (refused)                        │     │
│ └───────────────────┴─────────────────────────────────────┴────────────────────────────────────────┘     │
│                                                                             │
│ th-011 is not a retrieval failure at all — vector solves it and the graph route                          │
│ breaks it (see §1). Only th-006 is the shape the graph should win. That halves                           │
│ the size of the prize, and it means the reverse-lookup fix shou             │
│ th-006, th-004, th-005, 3h-005, ag-006 — not on th-011.                                                  │
│                                                                             │
│ Article ranges are extracted as endpoints only. GDPR Art. 83(5) yields                                   │
│ REFERENCES → Art. 12 and → Art. 22 but nothing for 13–21. An in             │
│ anchored on Art. 15 (the access right, which is what th-011 is about) would                              │
│ miss Art. 83(5). So even a correct inbound template does not serve th-011                                │
│ without range expansion at ingest. There is an alternate path —             │
│ LISTED_IN: "failure to provide access" → GDPR Art. 83(5) exists — but                                    │
│ LISTED_IN is also one of the six uncovered types.                                                        │
│                                                                                                          │
│ One more ceiling: chunking is paragraph-level (ADR-0003), so Art. 99(4)(a) and                           │
│ Art. 83(5)(b) are not addressable nodes. The best a reverse loo             │
│ the paragraph. th-006's grading rule asks to "connect it to Art. 16 via                                  │
│ Art. 99(4)(a)"; paragraph granularity may cap this at partially             │
│ could not determine from the code whether the judge accepts Art. 99(4) for                               │
│ Art. 99(4)(a) — that needs a judge probe, not a code read.                  │
│                                                                                                          │
│ ---                                                                                                      │
│ 3. Aggregation — the metadata already exists and nothing querie             │
│                                                                                                          │
│ Is there a structured filter path? No. The only vector SQL is search_sql()                               │
│ at src/query/retriever.py:151-180, whose sole predicate is                  │
│ WHERE {column} IS NOT NULL. Every other query against chunks in the codebase                             │
│ is a chunk_id = ANY(%s) hydration:                                                                       │
│ src/answer/answer_path.py:644, src/query/graph_path.py:124,                 │
│ src/index/embedder.py:259. No code path filters on article, paragraph, or                                │
│ regulation.                                                                                              │
│                                                                             │
│ What metadata is attached at chunk time? Everything needed, already typed and                            │
│ already loaded. src/index/schema.sql:26-61:                                                              │
│                                                                             │
│ article        INTEGER,                                                                                  │
│ article_title  TEXT,                                                                                     │
│ paragraph      INTEGER,                                                     │
│ regulation     TEXT NOT NULL,                                                                            │
│ shape          TEXT NOT NULL CHECK (shape IN ('paragraph','anne             │
│ citation_label TEXT NOT NULL UNIQUE,                                                                     │
│                                                                                                          │
│ data/processed/chunks-ai-act.jsonl confirms all 12 paragraphs o             │
│ stored as article=26, paragraph=1..12. The enumeration is one query:                                     │
│                                                                                                          │
│ SELECT chunk_id, citation_label, text FROM chunks                                                        │
│ WHERE regulation='AIA' AND article=26 AND shape='paragraph'                                              │
│ ORDER BY paragraph                                                          │
│                                                                                                          │
│ What it would take: a retrieve_by_article() beside retrieve() i             │
│ retriever.py, an enumeration intent detector, and a bypass in answer_path.                               │
│ There is no index on (regulation, article) but at 1,108 rows th             │
│                                                                                                          │
│ Important caveat — this may not be the cheapest fix. The graph already reaches                           │
│ this: uncapped cited 10 of 11 gold Art. 26 paragraphs on ag-001             │
│ COMPLETE, while the adopted budget cited 4. Raising or per-stratum-tuning the                            │
│ budget gets most of the aggregation win with a one-constant change. The                                  │
│ deterministic filter is the better long-term answer (it is exact, free, and                              │
│ cannot truncate); it is not the cheapest probe.                                                          │
│                                                                             │
│ A second blocker sits downstream of retrieval: MAX_TOKENS = 800 at                                       │
│ src/answer/generate.py:75. Five of the aggregation rows truncat             │
│ (ag-001, ag-004, ag-005, ag-009, ag-010). Article 26 alone is ~1,138                                     │
│ tokens of source. An 11-item enumeration with citation markup d             │
│ output tokens. Fixing retrieval without raising this budget will not move                                │
│ aggregation.                                                                │
│                                                                                                          │
│ ---                                                                                                      │
│ 4. Abstention — the premise is wrong, and the real cause is wor             │
│                                                                                                          │
│ Where is the decision made? Purely generator-side. src/answer/generate.py:77                             │
│ states it outright:                                                                                      │
│                                                                             │
│ ▎ THE PROMPT IS THE ONLY REFUSAL LEVER THIS PATH HAS.                                                    │
│                                                                             │
│ Rules 2 and 3 of SYSTEM_PROMPT (generate.py:109-114) carry the whole behaviour.                          │
│ There is no evidence gate on retrieval scores anywhere. The com             │
│ generate.py:79-82 records why: docs/metrics/query-path.md:459-467 measured                               │
│ rerank confidence at 0.747/0.796 on out-of-scope items against              │
│ well-answered one — no threshold separates them.                                                         │
│                                                                                                          │
│ The only mechanical control is post-hoc: citation_defect() at               │
│ eval/judge.py:176-195 and _cap() at :198-219 degrade correct_refusal →                                   │
│ partially_correct when a refusal row carries citations. That is scoring, not                             │
│ gating — it runs in the judge, after generation, and never changes an answer.                            │
│                                                                                                          │
│ The refusal regression is not caused by the graph                           │
│                                                                                                          │
│ Every out-of-scope and unanswerable row routed to vector with               │
│ graph_sent = 0 on all four systems. The graph never fires on them. It cannot                             │
│ be the cause of an 8/20 vs 11/20 difference.                                │
│                                                                                                          │
│ On route=vector with graph_sent=0, hybrid and rerank are the identical                                   │
│ pipeline — both field="reranked", reranks=True                              │
│ (eval/run_benchmark.py:98-101), both fed from the same committed artifact via                            │
│ replayed_passages(conn, field=spec["field"]) at run_benchmark.py:259.                                    │
│ documents_sent matches on all 71 such rows (0 mismatches). Yet:             │
│                                                                                                          │
│ ┌───────────────────────────────────────────────────┬───────────┐                                        │
│ │                                                   │                       │
│ ├───────────────────────────────────────────────────┼───────────┤                                        │
│ │ rows with identical pipeline and identical inputs │ 71        │                                        │
│ ├───────────────────────────────────────────────────┼──────────             │
│ │ identical answer text                             │ 26        │                                        │
│ ├───────────────────────────────────────────────────┼───────────┤                                        │
│ │ different answer text                             │ 45 (63%)              │
│ ├───────────────────────────────────────────────────┼───────────┤                                        │
│ │ different verdict                                 │ 9 (12.7%) │                                        │
│ └───────────────────────────────────────────────────┴──────────             │
│                                                                                                          │
│ despite temperature=0, seed=42 at src/answer/generate.py:251-25             │
│                                                                                                          │
│ Six of the nine verdict flips are refusal rows (hn-004, hn-006, oos-001,                                 │
│ oos-003, oos-009). oos-001 is the exact item you described — re             │
│ first sentence, then answered with 5 out-of-scope citations — and it is the                              │
│ hybrid label on an input byte-identical to rerank's, which refused cleanly.                              │
│                                                                             │
│ This puts a ~13% verdict-flip noise floor under every cross-config comparison in                         │
│ the benchmark. The spread is not merely statistically indistinguishable; a large                         │
│ part of it is the same system disagreeing with itself between r             │
│                                                                                                          │
│ Over-refusal on answerable strata is essentially flat — vector 10, rerank 6,                             │
│ hybrid 9, oracle 10 — but the identity of the refused rows chur             │
│ between vector and hybrid). Refusal is uncorrelated with answerability because it                        │
│ is a high-variance behavioural switch with no evidence input, not because a                              │
│ threshold is mistuned. You are right that it is not a threshold             │
│ it is a no-input problem compounded by sampling variance.                                                │
│                                                                                                          │
│ Feasibility of a pre-generation abstention gate: viable, but no             │
│ score — that is already measured dead. The signals that exist and are unused:                            │
│ whether the question's entities resolve in entity_linker (anchor_fillable and                            │
│ edge_reachable are already computed and recorded in selector-eval.jsonl),                                │
│ whether any retrieved citation_label shares a regulation with the question, and                          │
│ the gap between rank-1 and rank-5 rerank scores rather than the absolute value.                          │
│ All are cheap and all are already partly instrumented. This is worth building,                           │
│ but the nondeterminism must be fixed first — otherwise the gate's effect                                 │
│ cannot be measured above a 13% flip rate.                                   │
│                                                                                                          │
│ ---                                                                                                      │
│ 5. Citation precision — not what you think, and one layer deepe             │
│                                                                                                          │
│ Where is the cited list assembled? _citations_from() at                                                  │
│ src/answer/generate.py:301-393. Citations come from Cohere's na             │
│ spans (citation_options={"mode":"ENABLED"}, generate.py:250) — chunks the                                │
│ generator actually attributed, not the retrieved set. Unknown d             │
│ dropped and named (generate.py:355-364).                                                                 │
│                                                                             │
│ So the base metric is not conflated. Evidence: vector and rerank cite a mean                             │
│ of 2.05 unique chunks against a gold mean of 2.03, max 5 (the passage cap).                              │
│ The generator is genuinely selective.                                       │
│                                                                                                          │
│ The inflation is a graph provenance fan-out. src/answer/generate.py:369:                                 │
│                                                                                                          │
│ for chunk_id in doc.provenance or [doc.chunk_id]:                                                        │
│                                                                             │
│ One graph statement the model cites emits one Citation row per asserting chunk                           │
│ in its provenance (up to MAX_PROVENANCE = 3, src/answer/path_to             │
│ Because the graph stores one relationship per asserting chunk, a hot fact is                             │
│ asserted by many chunks — cypher_templates.py:60-63 records tha             │
│ high risk ai system -[:CLASSIFIED_AS]-> high risk carries the same 124 chunk ids                         │
│ on all 169 rows.                                                                                         │
│                                                                             │
│ Decomposing ag-001 under hybrid:                                                                         │
│                                                                                                          │
│ ┌──────────────────────────┬───────────────────────────────────             │
│ │                          │                                                        │                    │
│ ├──────────────────────────┼────────────────────────────────────────────────────────┤                    │
│ │ distinct prose spans     │ 31                                             │
│ ├──────────────────────────┼────────────────────────────────────────────────────────┤                    │
│ │ Citation rows emitted    │ 51                                                     │                    │
│ ├──────────────────────────┼───────────────────────────────────             │
│ │ unique chunks cited      │ 29 (gold: 11)                                          │                    │
│ ├──────────────────────────┼────────────────────────────────────────────────────────┤                    │
│ │ of which GRAPH-sourced   │ 25 — Arts. 23, 71, 73, 95, 111, 43             │
│ ├──────────────────────────┼────────────────────────────────────────────────────────┤                    │
│ │ of which PASSAGE-sourced │ 5                                                      │                    │
│ └──────────────────────────┴───────────────────────────────────             │
│                                                                                                          │
│ That is your "29 citations spanning Arts. 13, 23, 71, 73 and 95", precisely. The                         │
│ vector half cited 5 chunks, three of them Art. 26. The graph half added 25 chunks                        │
│ of provenance noise.                                                                                     │
│                                                                                                          │
│ Fan-out ratio (Citation rows per distinct span): vector 1.20, rerank 1.18,                               │
│ hybrid 1.31, oracle 1.38. Chunk precision falls from 0.572 (vector) to                                   │
│ 0.521 (oracle) as more graph fires.                                                                      │
│                                                                                                          │
│ So: the metric is honest for the vector arms and inflated for the graph arms,                            │
│ by a mechanism that attributes to the model chunks it never saw as text. A                               │
│ graph statement's provenance is evidence for the statement, not what the                                 │
│ generator used. Collapsing the fan-out to provenance[0], or carrying provenance                          │
│ as a separate field from citations, fixes it.                                                            │
│                                                                             │
│ ---                                                                                                      │
│ 6. Eval harness integrity — better than it looks, but the published table is the wrong one               │
│                                                                             │
│ The harness already solves the denominator problem, and the report already says                          │
│ so. scoreboard() at eval/run_benchmark.py:437 computes common_ids as the                                 │
│ intersection of scorable ids across systems (:466-467) and repo             │
│ common_pass/common_n (:554-556). _report() prints it at :697-707 with an                                 │
│ explicit warning:                                                                                        │
│                                                                             │
│ ▎ The per-system column is NOT comparable across systems: each drops its own                             │
│ ▎ errored and MAX_TOKENS rows and they are different rows. COMMON is.                                    │
│                                                                                                          │
│ Drops are logged with a reason — errors, truncated, ungraded lists at                                    │
│ :543-550, printed at :763-768. Nothing is silently swallowed. The try/except                             │
│ at :287-297 records the failure as the row's result and prints it.                                       │
│                                                                                                          │
│ The problem is that markdown_table() (:606-685) — the table tha             │
│ README — publishes per_stratum with the drifting per-system denominators. The                            │
│ comparable numbers exist and are not what gets published.                                                │
│                                                                             │
│ scorable() at :454-463 excludes rows that are errored, MAX_TOKENS, in the                                │
│ expected_fail bucket, or unverdicted. Reproducing your denominators exactly:                             │
│                                                                             │
│ ┌────────┬──────────┬──────────┬───────────────────────────────────────────┐                             │
│ │ system │   rows   │ excluded │                    why                     │
│ ├────────┼──────────┼──────────┼───────────────────────────────────────────┤                             │
│ │ vector │ 100 → 97 │ 3        │ 3h-002 canary, 3h-006 + ag-010             │
│ ├────────┼──────────┼──────────┼───────────────────────────────────────────┤                             │
│ │ rerank │ 100 → 94 │ 6        │ canary, 4× MAX_TOKENS, sh-020 API error   │                             │
│ ├────────┼──────────┼──────────┼───────────────────────────────             │
│ │ hybrid │ 100 → 96 │ 4        │ canary, 3× MAX_TOKENS                     │                             │
│ ├────────┼──────────┼──────────┼───────────────────────────────────────────┤                             │
│ │ oracle │ 100 → 93 │ 7        │ canary, 6× MAX_TOKENS                     │                             │
│ └────────┴──────────┴──────────┴───────────────────────────────────────────┘                             │
│                                                                             │
│ The exclusion is quality-correlated and it inflates accuracy. Of the 20                                  │
│ excluded rows, 16 are partially_correct or wrong and only 3 are             │
│ MAX_TOKENS truncation is not random — it hits the longest answers, which are the                         │
│ aggregation rows, which is precisely why aggregation reads 10/7             │
│ is silently deleting the hardest stratum's failures.                                                     │
│                                                                                                          │
│ Correct comparable figures (the 89 rows every system scored):               │
│                                                                                                          │
│ ┌────────┬───────────────┬────────────────────┐                                                          │
│ │ system │   published   │ common denominator │                             │
│ ├────────┼───────────────┼────────────────────┤                                                          │
│ │ vector │ 39/97 (40.2%) │ 35/89 (39.3%)      │                                                          │
│ ├────────┼───────────────┼────────────────────┤                             │
│ │ rerank │ 40/94 (42.6%) │ 37/89 (41.6%)      │                                                          │
│ ├────────┼───────────────┼────────────────────┤                                                          │
│ │ hybrid │ 35/96 (36.5%) │ 31/89 (34.8%)      │                             │
│ ├────────┼───────────────┼────────────────────┤                                                          │
│ │ oracle │ 33/93 (35.5%) │ 30/89 (33.7%)      │                                                          │
│ └────────┴───────────────┴────────────────────┘                             │
│                                                                                                          │
│ Same conclusion, and the hybrid looks slightly worse. Flag: fixing the drift                             │
│ lowers every published number. This is a fix that makes the met             │
│ system no different. So does un-excluding MAX_TOKENS rows.                                               │
│                                                                                                          │
│ The judge is not the problem. eval/judge.py: temperature 0 (:329),                                       │
│ JUDGE_MAX_TOKENS=300 (:98), a four-verdict rubric with the grading rule given                            │
│ precedence (SYSTEM_PROMPT, :104-141), deterministic post-hoc citation capping                            │
│ (:198-219), and an unparseable reply raised as JudgeError rather than                                    │
│ defaulted (:222). Verdicts are stored per item — verdict, judge_reason,                                  │
│ judge_defect, judge_capped_from, judge_attempts, judge_cost_usd are all in                               │
│ benchmark.jsonl.                                                                                         │
│                                                                                                          │
│ Agreement against your 20 hand labels in eval/judge-agreement.jsonl:                                     │
│                                                                                                          │
│ - exact agreement 17/20 (85%)                                                                            │
│ - binary pass/fail agreement 19/20 (95%) — the axis accuracy is computed on                              │
│                                                                                                          │
│ Only th-016 flips the pass/fail bit. At n=20 the interval is wi             │
│ is clearly not the dominant noise source. Generation nondeterminism at a 12.7%                           │
│ verdict-flip rate is an order of magnitude larger than judge disagreement.                               │
│                                                                             │
│ ---                                                                                                      │
│ Ranked findings — expected accuracy gain per unit of work                                                │
│                                                                             │
│ Tier 1 — cheap, high confidence                                                                          │
│                                                                                                          │
│ 1. Generation is nondeterministic; the benchmark has a ~13% fli             │
│ floor. 45/71 identical-input pairs produce different text, 9/71 flip the                                 │
│ verdict, 6 of those on refusal rows. Nothing else in this list can be measured                           │
│ until this is bounded. Cost: run one system twice and difference it. This does                           │
│ not raise accuracy — it makes accuracy measurable. Do this first.                                        │
│                                                                                                          │
│ 2. Publish common_pass/common_n in markdown_table(). The correct                                         │
│ computation already exists at run_benchmark.py:554-556. One function edit.                               │
│ Lowers every published number by ~1pp and makes cross-config comparison valid.                           │
│ Flag: improves the metric's honesty, not the system.                                                     │
│                                                                                                          │
│ 3. Raise MAX_TOKENS from 800 (generate.py:75) and re-run aggreg             │
│ Five aggregation rows truncate. Cheap, and a prerequisite for any aggregation                            │
│ work — retrieval fixes cannot show up through a truncated answer.                                        │
│                                                                             │
│ 4. Tune the graph budget per stratum, or raise DEFAULT_BUDGET_N.                                         │
│ ADOPTED_BUDGET="first" at context_assembly.py:145 was fitted on 23 rows.                                 │
│ uncapped reaches 68.6% gold retention vs 54.9%, and cites 10/11             │
│ vs 4/11. One constant. This is the highest expected gain per unit of work in the                         │
│ whole list — but it is confounded with finding 1 until that is              │
│                                                                                                          │
│ 5. Collapse the graph citation fan-out (generate.py:369). Restores citation                              │
│ precision on the graph arms. Flag: this improves the precision              │
│ improving answers — it stops over-counting rather than grounding better.                                 │
│                                                                                                          │
│ Tier 2 — moderate cost, moderate confidence                                                              │
│                                                                                                          │
│ 6. Add an inbound REFERENCES template. 1,161 loaded edges, curr             │
│ unreachable. AIA Art. 99(4) → AIA Art. 16 is present and is exactly th-006's                             │
│ missing gold. This is the narrow claim worth testing — the shap             │
│ structurally cannot serve. But the prize is ~1–5 items, not a stratum, and                               │
│ th-011 is not among them.                                                   │
│                                                                                                          │
│ 7. Deterministic enumeration path. article/paragraph/regulation             │
│ already indexed columns (schema.sql:31-33); nothing queries them. Exact and                              │
│ free once built. Compare against finding 4 first — the budget change may capture                         │
│ most of it for a fraction of the work.                                      │
│                                                                                                          │
│ 8. Expand article ranges at ingest. "Articles 12 to 22" currently yields only                            │
│ endpoint edges. Required before finding 6 can serve th-011-shaped questions.                             │
│                                                                                                          │
│ Tier 3 — speculative                                                        │
│                                                                                                          │
│ 9. Pre-generation abstention gate on non-score signals (entity              │
│ success, regulation match, rank-1-to-rank-5 gap). Rerank score is already proven                         │
│ dead. Blocked on finding 1.                                                 │
│                                                                                                          │
│ 10. Sub-paragraph addressability for Art. 99(4)(a) / Art. 83(5)(b).                                      │
│ Contradicts ADR-0003. Expensive, and may be unnecessary — verif             │
│ tolerance first.                                                                                         │
│                                                                                                          │
│ The bottom line on the graph                                                                             │
│                                                                                                          │
│ The narrow claim you set — the graph should beat vector on reve             │
│ lookup and enumeration — is partly supported by the data and wholly unsupported                          │
│ by the system as built. The substrate is real: 1,161 loaded REF             │
│ containing both target answers, and 13 gold chunks the graph reaches that vector                         │
│ misses. But as shipped the graph has produced zero wins and fou             │
│ 28 fired rows, costs 85% more, and degrades citation precision.                                          │
│                                                                             │
│ My recommendation: do not cut it yet — but put it on a deadline. Findings 4                              │
│ and 6 are the whole case for the graph, they are cheap, and they are the last two                        │
│ untested. If a per-stratum budget plus an inbound REFERENCES te             │
│ produce a measurable win on ag-001/ag-005/th-006 above the noise floor from                              │
│ finding 1, then the graph cannot win where it structurally should, and §1's                              │
│ evidence says it will not win anywhere else. Cut it then, and k             │
│ pipeline for the citation-label metadata alone.                                                          │
│                                                                                                          │
│ ---                                                                                                      │
│ The three experiments I would run first                                                                  │
│                                                                             │
│ E1 — Bound the noise floor (blocks everything else)                                                      │
│                                                                             │
│ Re-run one system (rerank, cheapest at ~$0.65) twice on all 100 rows with no                             │
│ code change. Difference the verdicts pairwise.                              │
│                                                                                                          │
│ Separates: real config differences vs sampling variance. If the             │
│ reproduces at ~13%, then the entire 40.2 / 42.6 / 36.5 / 35.5 spread is inside                           │
│ run-to-run noise and every subsequent experiment needs paired runs or n≫100.                             │
│ Also probe whether Cohere honours seed for command-a-03-2025 —              │
│ the observed variance means the payload is not actually identical and I am wrong                         │
│ about the mechanism; that is the falsifier for §4.                                                       │
│                                                                                                          │
│ Cost: one sweep. Decides: whether any other measurement is trustworthy.                                  │
│                                                                             │
│ E2 — Budget ablation on the graph-fired rows only                                                        │
│                                                                             │
│ Re-run hybrid on just the 28 rows where graph_sent > 0, under first(50) vs                               │
│ uncapped, with MAX_TOKENS raised to 2000 so truncation does not             │
│                                                                                                          │
│ Separates the three competing explanations for why the graph loses:                                      │
│ (a) the graph retrieves the wrong things → uncapped does not he             │
│ (b) the budget discards the right things → uncapped wins, and finding 4 is the fix;                      │
│ (c) the graph is distracting at any volume → uncapped is worse, and the fix is                           │
│ to send fewer, better statements, not more.                                                              │
│                                                                                                          │
│ answer-eval.jsonl already predicts (b) at 68.6% vs 54.9% retent             │
│ 23 rows without the token fix. This is the decisive test of whether the hybrid is                        │
│ salvageable.                                                                │
│                                                                                                          │
│ Cost: 56 generations. Decides: the entire Tier-1 ranking.                   │
│                                                                                                          │
│ E3 — Inbound REFERENCES probe, offline, no generation                                                    │
│                                                                             │
│ Before writing a template: run the inbound query directly against Neo4j for each                         │
│ gold chunk of the reverse-lookup and penalty-tier items (th-006, th-004,                                 │
│ th-005, 3h-005, ag-006, th-011):                                            │
│                                                                                                          │
│ cypher                                                                                                   │
│ MATCH (p:Article)-[r:REFERENCES]->(:Article {canonical_name: $a             │
│ RETURN p.canonical_name, r.source_chunk_id                                                               │
│                                                                                                          │
│ Measures: how many gold chunks an inbound traversal recovers th             │
│ misses — the true ceiling on finding 6, before any implementation cost.                                  │
│                                                                                                          │
│ I predict it recovers aia-art99-para4 for th-006 and fails on t             │
│ because of the range-expansion gap. If that prediction holds, the reverse-lookup                         │
│ prize is one to five items and should be scheduled accordingly              │
│ stratum-level fix.                                                                                       │
│                                                                             │
│ Cost: zero generation, zero API spend, containers only. Run this before                                  │
│ committing to any graph work.                                                                            │
│                                                                             │
│ ---                                                                                                      │
│ What I could not determine from the code                                                                 │
│                                                                             │
│ - Whether Cohere honours seed for command-a-03-2025. The nondeterminism is                               │
│ measured from artifacts; the mechanism is inferred. E1 tests it.                                         │
│ - Whether the judge accepts Art. 99(4) for a rule demanding Art. 99(4)(a).                               │
│ This sets the ceiling on the reverse-lookup fix and needs a judge probe.                                 │
│ - Live graph state. All graph facts come from data/processed/ex             │
│ and graph-load-report.json, which record what was written. I did not query                               │
│ Neo4j or Postgres — the containers were not started (read-only              │
│ - Why sh-020 errored beyond the recorded UnprocessableEntityError from the                               │
│ Cohere API. One row, rerank only.