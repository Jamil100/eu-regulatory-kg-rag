# Eval set metrics

Status: **23 of a target 100 questions, all verified, all self-consistent against the graph.**
21 are scoreable for retrieval recall, which clears ADR-0004's "~20 labeled queries" and unblocks the
embedding-dimension experiment.

Third companion to `extraction-cost-and-findings.md` (what came out of the model) and
`graph-load.md` (what came out of the loader). This measures **the instrument itself** — the question
set every Phase 5 benchmark claim will be computed from. It is documented to the same standard as the
pipeline because a benchmark is only as trustworthy as its labels.

Regenerate with:

```bash
pytest tests/test_eval_questions.py       # every claim below is asserted here
```

---

## Shape

| | |
|---|---|
| Questions | **23** (target 100) |
| Verified | **23 / 23** |
| Scoreable for recall (non-empty gold chunks) | **21** |
| Distinct gold chunks | **40** |
| Gold chunk references | 51 |
| Mean gold chunks per scoreable question | **2.4** |
| Corpus coverage | **40 of 1,108 chunks — 3.6%** |

Fields per row: `id, stratum, hops, question, gold, citations, source_chunk_ids, grading_rule,
ontology_edges, must_cite, verified`, plus optional `canary`, `note`, `expected_fail`,
`graph_traversable` / `graph_traversable_reason`.

`source_chunk_ids` is the field that matters most and the one the previous set
(`eval/questions.jsonl`, 6 rows) did not have at all. Without gold passages there is no recall metric,
and ADR-0004 stays *Proposed* forever.

## Strata against target

Targets are roadmap §5.3 as revised 2026-07-31 — 8 strata, total 100.

| Stratum | Have | Target | Cites? |
|---|---|---|---|
| single-hop | 6 | 20 | yes |
| two-hop | 4 | 20 | yes |
| three-hop | 2 | 15 | yes |
| cross-regulation | 4 | 15 | yes |
| aggregation | 3 | 10 | yes |
| out-of-scope | 1 | 5 | **no citation** |
| unanswerable | 1 | 5 | **no citation** |
| hard-negative | 2 | 10 | **must cite** |
| **Total** | **23** | **100** | |

Hop distribution: 9 one-hop, 9 two-hop, 3 three-hop, 2 zero-hop (refusals).

### The three refusal modes are separate strata on purpose

They are three different behaviours with three different correct outputs, and averaging them into one
"refusal rate" hides which one failed:

| Mode | Situation | Correct behaviour |
|---|---|---|
| `out-of-scope` | Outside the corpus entirely (US law) | Refuse, name the scope limit, **cite nothing** |
| `unanswerable` | In corpus, but the text states no such fact | Refuse by naming the **specific absence**, cite nothing |
| `hard-negative` | The premise is false | Reject the premise and **cite** the text that corrects it |

`tests/test_eval_questions.py` enforces `(must_cite, has_gold_chunks)` per mode rather than as one
global rule. The global rule would pass today — but it would also wave through an `out-of-scope` row
that wrongly carried gold chunks.

## Coverage is narrow, and that is the honest caveat

The 40 gold chunks are **3.6% of the corpus** and concentrate hard:

| Article | Gold chunks |
|---|---|
| AIA Art. 26 (deployer obligations) | 11 |
| AIA Art. 9 (risk management) | 6 |
| AIA Art. 99 (penalties) | 4 |
| GDPR Art. 83 (fines) | 3 |
| AIA Art. 3 (definitions) | 3 |
| AIA Art. 6 (classification) | 3 |

Regulation split: **34 AIA / 6 GDPR** — the set is AI-Act-heavy, so GDPR-side retrieval is close to
unmeasured.

**What this is and is not good for.** It is sound for the ADR-0004 **comparison** (1536 vs 512), where
both arms see exactly the same slice and the difference is what is being measured. It is thin as an
**absolute** retrieval claim, and a recall@10 number computed here should be reported with the 3.6%
attached. Do not let a "<2% recall loss" threshold imply more precision than 21 queries over 40 chunks
can support.

Ten chunks are reused across questions (`gdpr-art83-para5` in three rows), which is realistic — the
same paragraph genuinely answers several questions — but it further narrows the effective sample.

## Ontology coverage

Relationship types declared across the set, i.e. what the graph path is actually exercised on:

`APPLIES_TO` 9 · `REFERENCES` 9 · `IMPOSES` 8 · `LISTED_IN` 4 · `INTERACTS_WITH` 3 ·
`SETS_PENALTY` 3 · `DEFINED_IN` 2 · `EXEMPT_FROM` 2 · `CLASSIFIED_AS` 2 · `PENALIZED_UNDER` 2 ·
`PERMITS` 1 · `ENFORCED_BY` 1

**`GRANTS` is the one ontology type no question exercises.** It is 86 edges in the graph covering
rights-conferring provisions (GDPR Ch. III), and nothing in the eval set will notice if it breaks.
That is the same "untested type = possible ontology hole" argument that justified the Step 0
`PENALIZED_UNDER` probe — worth a question when the set expands.

## Special row markers

| Marker | Rows | Meaning |
|---|---|---|
| `expected_fail` | `3h-002` | Declares `EXEMPT_FROM`; the extractor emits `PERMITS` on the Art. 6(3) derogation. Known-red on purpose, reported in its own bucket. |
| `graph_traversable: false` | `xr-003`, `xr-004` | AIA Art. 99 and GDPR Art. 83 never cross-cite, so no article-level bridge is derivable. Vector-path-only; the hybrid correctly shows no advantage. |

Both are deliberate and both are asserted. `expected_fail` carries a reverse check: if every declared
edge later exists, the test **fails** and tells you to remove the flag, so a silenced canary cannot
outlive its cause.

## Defects found on first mechanical check (2026-07-31)

The golds were sound; the metadata was not. All 40 gold chunk ids existed, and 16 spot-checked factual
claims (fine ceilings, the Annex VI / notified-body split, the narrow-procedural-task derogation, three
definition chunks) matched the source text exactly.

| Defect | Count | Detail |
|---|---|---|
| Rows declaring graph edges their gold chunks do not carry | **10 of 23** | 4 were one systematic error: `PENALIZED_UNDER` (Obligation→Article) used where `SETS_PENALTY` (Article→Penalty) was meant |
| Gold chunk that did not contain its own answer | **1** | `hn-001` asked for the prohibited-practice fine and pointed at AIA Art. 99(1), the general Member-State penalties provision, which states no figure. Repointed to Art. 99(3). |
| Harness pointing at a deleted file | **1** | `run_benchmark.py` loaded `questions.jsonl` after the set was renamed |
| Strata conflating distinct behaviours | **1** | `oos-002` is unanswerable-from-text, not out-of-scope |
| Numbering gap | **1** | `hn-002` absent |

**The systematic one is the interesting one.** Five rows used `PENALIZED_UNDER` for "what is the
maximum fine under Article X". That edge runs Obligation→Article; the question wants Article→Penalty,
which is `SETS_PENALTY`. One row (`th-004`) had it inverted the other way. A question labelled with the
wrong edge makes a working graph look broken — or, worse, hides a real gap behind a plausible label.

## Open

- **77 questions short of target.** Every stratum is under, and the roadmap calls the gold-writing
  "~2 evenings, non-negotiable".
- **GDPR-side retrieval is nearly unmeasured** — 6 of 40 gold chunks.
- **`GRANTS` is unexercised** by any question.
- **Judge agreement is unmeasured.** Roadmap §5.3 asks for a hand-verified 20% sample and a reported
  agreement figure; `eval/judge.py` is still a stub.
- **`gdpr-art70-para1` is unreachable by the graph path** — the 864-token EDPB paragraph never
  extracted, so any question about "which authority does what" is vector-only by necessity.
