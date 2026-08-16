# Eval set metrics

Status: **100 of a target 100 questions, all verified, all self-consistent against the graph.**
90 are scoreable for retrieval recall. The set reached target on **2026-08-15**; it stood at 23 from
2026-07-31, and the Phase-3 plan called 23 *"non-blocking for Phase 3, blocking at Phase 5 — the
strata are the measurement, so the benchmark cannot be run at 23."*

**The 77 rows added on 2026-08-15 have not been through a live sweep.** Every claim in this file is
about the *labels* and is asserted by `tests/test_eval_questions.py`, which needs no database. The
retrieval artifacts (`rerank-eval.jsonl`, `router-eval.jsonl`, `selector-eval.jsonl`,
`answer-eval.jsonl`) are still keyed to the 23-row set, so **every figure computed from them has the
old denominator and is not comparable to anything here** until those sweeps are re-run. See §Open.

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

| | at 23 | **at 100** |
|---|---|---|
| Questions | 23 | **100** (target 100) |
| Verified | 23 / 23 | **100 / 100** |
| Scoreable for recall (non-empty gold chunks) | 21 | **90** |
| Distinct gold chunks | 40 | **99** |
| Gold chunk references | 51 | **203** |
| Mean gold chunks per scoreable question | 2.4 | **2.3** |
| Corpus coverage | 40 of 1,108 — 3.6% | **99 of 1,108 — 8.9%** |

Fields per row: `id, stratum, hops, question, gold, citations, source_chunk_ids, grading_rule,
ontology_edges, must_cite, verified, route`, plus optional `canary`, `note`, `expected_fail`,
`route_reason`, `graph_traversable` / `graph_traversable_reason`.

`source_chunk_ids` is the field that matters most and the one the previous set
(`eval/questions.jsonl`, 6 rows) did not have at all. Without gold passages there is no recall metric,
and ADR-0004 stays *Proposed* forever.

## Strata against target

Targets are roadmap §5.3 as revised 2026-07-31 — 8 strata, total 100.

| Stratum | Have | Target | Cites? |
|---|---|---|---|
| single-hop | **20** | 20 | yes |
| two-hop | **20** | 20 | yes |
| three-hop | **15** | 15 | yes |
| cross-regulation | **15** | 15 | yes |
| aggregation | **10** | 10 | yes |
| out-of-scope | **5** | 5 | **no citation** |
| unanswerable | **5** | 5 | **no citation** |
| hard-negative | **10** | 10 | **must cite** |
| **Total** | **100** | **100** | |

Hop distribution: 38 one-hop, 40 two-hop, 16 three-hop, 6 zero-hop (refusals).

### Route labels, and the constraint they are under

`route` is the hand-verified gold the router is scored against. The distribution is deliberate, not
incidental: `tests/test_eval_questions.py::test_route_labels_are_not_a_single_class` requires all
three routes to appear and the majority class to stay **under 60%**, because a constant router that
beat the real one would make the comparison meaningless.

| Route | Rows | Where they come from |
|---|---|---|
| `vector` | 48 | every single-hop row, every refusal row, and the 6 non-traversable cross-regulation rows |
| `both` | 48 | every two-hop and three-hop row, 11 cross-regulation rows, 3 aggregation rows |
| `graph` | 4 | `ag-001`, `ag-004`, `ag-005`, `ag-006` — enumerations no top-k draw can return |

Writing the set stratum-by-stratum violates that invariant *mid-build* even when the end state
satisfies it: the single-hop batch alone put `vector` at 73%. The batches were therefore interleaved
so the suite stayed green throughout, which is worth recording because the obvious build order does
not work.

`graph` never arises from the mechanical seed in `_seed_route()` — it is always a hand override
carrying a `route_reason`, and all four are aggregation rows whose answer is an enumeration spread
across articles.

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

## Coverage is still narrow, and that is still the honest caveat

The 99 gold chunks are **8.9% of the corpus** — up from 3.6%, and still a slice. They concentrate
less than they did, but they do still concentrate:

| Article | Gold chunks |
|---|---|
| AIA Art. 26 (deployer obligations) | 11 |
| AIA Annex III (high-risk areas) | 8 |
| AIA Art. 9 (risk management) | 6 |
| AIA Art. 99 (penalties) | 6 |
| GDPR Art. 83 (fines) | 4 |
| AIA Art. 3 (definitions) | 3 |
| GDPR Art. 35 (DPIA) | 3 |
| GDPR Art. 22 (automated decisions) | 3 |
| AIA Art. 6 (classification) | 3 |
| AIA Art. 50 (transparency) | 3 |

Regulation split: **66 AIA / 33 GDPR** — up from 34/6. The GDPR side is no longer close to
unmeasured, which was the expansion's first stated goal; at 1:2 the set remains AI-Act-leaning, which
is defensible for a repo about the AI Act but should not be read as balanced coverage of the GDPR.

**What this is and is not good for.** At 90 scoreable questions over 99 chunks it can carry a
per-stratum benchmark claim, which 21 over 40 could not. It still cannot carry an *absolute* retrieval
claim about the corpus: 91% of the corpus is in no question's gold set, so a recall figure computed
here is recall over the slice someone chose to ask about. Report the 8.9% beside it.

53 chunks are reused across questions, which is realistic — the same paragraph genuinely answers
several questions — but it narrows the effective sample below what 99 distinct chunks suggests.

## Ontology coverage

Relationship types declared across the set, i.e. what the graph path is actually exercised on:

`IMPOSES` 58 · `APPLIES_TO` 19 · `SETS_PENALTY` 15 · `LISTED_IN` 14 · `CLASSIFIED_AS` 13 ·
`REFERENCES` 12 · `INTERACTS_WITH` 12 · `GRANTS` 12 · `PERMITS` 10 · `DEFINED_IN` 9 ·
`EXEMPT_FROM` 7 · `ENFORCED_BY` 4 · `PENALIZED_UNDER` 3

**`GRANTS` is now exercised, and that closes the one open ontology hole.** It was 86 edges over
rights-conferring provisions (GDPR Ch. III) that no question would have noticed breaking — the same
"untested type = possible ontology hole" argument that justified the Step 0 `PENALIZED_UNDER` probe.
Twelve rows now declare it, and `ag-004` makes it a *traversal target*: all seven of its gold chunks
carry `GRANTS`, so a regression in `GRANTS` extraction shows up on that row and nowhere else. The row
carries a `canary` field saying so.

**Every one of the 13 ontology types is now declared by at least one question.** `ENFORCED_BY` (4) and
`PENALIZED_UNDER` (3) are the thinnest and are the ones to watch next.

## Special row markers

| Marker | Rows | Meaning |
|---|---|---|
| `expected_fail` | `3h-002` | Declares `EXEMPT_FROM`; the extractor emits `PERMITS` on the Art. 6(3) derogation. Known-red on purpose, reported in its own bucket. |
| `graph_traversable: false` | `xr-003`, `xr-004`, `xr-012`, `xr-013`, `xr-014`, `xr-015` | The two regulations address the same subject and never cross-cite, so no article-level bridge is derivable. Vector-path-only; the hybrid correctly shows no advantage. |

**The non-traversable set grew from 2 rows to 6, and it was verified rather than assumed.** The four
added rows pair AIA Arts. 13, 14, 15 and 73 against GDPR Arts. 13, 22, 5 and 33. A scan confirmed that
none of those AIA articles cites `2016/679` in any paragraph, and that none of those GDPR articles
cites `2024/1689` or names the AI Act — in the GDPR's case necessarily, since it was adopted eight
years earlier and **cites the AI Act nowhere at all**. The bridge only ever runs AIA → GDPR, which is
a structural fact about this corpus worth stating once: every `INTERACTS_WITH` edge between the two
regulations originates on the AI Act side.

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

- ~~**77 questions short of target.**~~ Closed 2026-08-15; the set is at 100.
- ~~**GDPR-side retrieval is nearly unmeasured** — 6 of 40 gold chunks.~~ Closed; now 33 of 99.
- ~~**`GRANTS` is unexercised** by any question.~~ Closed; 12 rows declare it and `ag-004` makes it a
  traversal target.

- **THE RETRIEVAL ARTIFACTS ARE STALE AND EVERY NUMBER COMPUTED FROM THEM IS ON THE OLD
  DENOMINATOR.** `rerank-eval.jsonl`, `router-eval.jsonl`, `selector-eval.jsonl` and
  `answer-eval.jsonl` each hold 23 rows keyed to the old ids. Until they are re-swept, the published
  `21/22` router accuracy, the `24 of 32` selector yield, the `POST[5] = 27 of 51` rerank ceiling and
  every oracle in `answer-path.md` describe a set that no longer exists. Sixteen tests fail on exactly
  this and they are meant to: they are the artifacts announcing their own staleness rather than
  quietly re-scoring. Re-run, with containers up and a key:

  ```bash
  python -m src.query.reranker          --eval --refresh   # blocking for the benchmark
  python -m src.query.router            --eval --refresh   # else ADR-0012's numbers stay stale
  python -m src.query.template_selector --eval --refresh   # else ADR-0013's do
  python -m src.answer.answer_path --prereg                # free, but needs the containers
  ```

- ~~**The 77 new rows have never been retrieved against.**~~ Closed 2026-08-15 by
  `python -m src.query.reranker --eval --refresh`. The answer is below, and it is the most useful
  thing the expansion produced.

### The retrieval ceiling, measured — and it predicts the benchmark before the benchmark ran

**46 of 203 gold references (22.7%) are not in the 50-candidate pool at all**, so no reranker and no
value of `k ≤ 50` can reach them. The vector path's ceiling is 157/203, not 203/203. What matters is
*where* the loss falls:

| Stratum | Gold unreachable at k≤50 | |
|---|---|---|
| single-hop | **0 of 24** | 0.0% |
| hard-negative | **0 of 13** | 0.0% |
| two-hop | 7 of 42 | 16.7% |
| three-hop | 11 of 44 | 25.0% |
| cross-regulation | 11 of 32 | 34.4% |
| aggregation | 17 of 48 | 35.4% |

This is the hybrid thesis stated as a retrieval fact rather than as an argument. Embedding search
loses nothing on the stratum where the question and the answering passage share vocabulary, and loses
a third of the gold on the strata that require assembling an answer across provisions. Roadmap §4
puts it as *"embeddings capture semantic similarity but have no notion of structure"*; this is that
claim with a denominator.

**Three rows lose 100% of their gold** and are therefore unanswerable-with-grounding by the
vector-only and rerank systems, whatever the generator does:

| Row | Stratum | Why |
|---|---|---|
| `ag-008` | aggregation | 8/8. "Which areas of use does Annex III cover?" is abstract; the Annex III points are concrete lists of system types, and the two share almost no vocabulary. `LISTED_IN` reaches them in one traversal. |
| `xr-007` | cross-regulation | 2/2. "Does the AI Act override the GDPR?" — the answering text is a scope provision that never uses the word "override". |
| `th-004` | two-hop | 1/1. Pre-existing; ADR-0012 already records this row as the one the router misroutes. |

These are a *result*, not a defect in the questions: they are the cases the graph path exists for, and
all three are routed so that it can contribute. They should be read alongside the six
`graph_traversable: false` rows, which are the same finding from the opposite side — the set now
contains both rows no vector draw can answer and rows no traversal can answer, and neither system
wins everywhere.

**Caveat on the latency figures from that sweep.** 16 of 100 rerank calls stalled in tenacity backoff
against the rate-limited key, up to 88 s. The recall numbers are unaffected — an ordering is an
ordering whenever it arrives — but the rerank p95 from that run measures the API key and not the
model. Quote the p50 (270 ms) and the stall list, as the harness itself prints.
- **Judge agreement is unmeasured.** `eval/judge.py` is no longer a stub, but roadmap §5.3's
  hand-verified 20% sample has not been graded: `eval/judge-agreement.jsonl` does not exist yet, and
  the holdout must be graded by hand **before** anyone reads a judge verdict for it to mean anything.
- **`gdpr-art70-para1` is unreachable by the graph path** — the 864-token EDPB paragraph never
  extracted, so any question about "which authority does what" is vector-only by necessity.
- **`hn-001` carries a stale `note`.** It reads "Awaiting sign-off — verified stays false" while the
  row's `verified` field is `true`. The sign-off happened; the note did not get updated. Harmless to
  every test, but it is the kind of drift this file exists to catch.
- **One row reaches no graph node, and that is correct.** `oos-005` (China's deep synthesis rules)
  links nothing in the EU corpus, which is the right outcome for an out-of-scope question — the other
  four out-of-scope rows *do* link, because they name concepts the EU corpus also uses. Refusal
  therefore cannot be delegated to "retrieval found nothing".
