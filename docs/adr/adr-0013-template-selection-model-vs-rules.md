# ADR 0013: Select templates with deterministic rules; R7B could not fill the parameters

- **Status:** Accepted
- **Date:** 2026-08-04
- **Deciders:** Jamil (project owner)
- **Context phase:** Phase 3 — Step 5, graph query path

## Context

ADR-0002 fixed the shape of this stage three phases ago: *"A fixed library of ~6
parameterized Cypher templates; the model only chooses a template and fills
parameters."* Step 5 is the first step that actually needs a chooser, so it is the
first chance to test the second half of that sentence — **fills parameters** —
against a real graph.

ADR-0012 had just run the same experiment one stage earlier and the rules won
21 of 22 to R7B's 10 of 22, below the majority-class constant. Re-running it here
was a deliberate choice rather than a formality: routing and selection are
different tasks, and ADR-0002 names the model explicitly for *this* one.

## What was fixed before the measurement ran

1. **Gold yield is the metric**: does the executed plan's provenance contain the
   row's gold `source_chunk_ids`? Same ground truth Step 4 scored the vector path
   on.
2. **Every constant arm is reported** — one per template, as ADR-0012 reported
   `always-vector`. A selector that cannot beat a constant has not earned a place
   in the request path.
3. **The oracle is computed first**: the best single `(template, anchor)` pair per
   row, chosen with the gold visible, so a null result can be attributed to
   selection rather than to the graph not holding the gold.
4. **The denominator is the routed set** — the 9 rows the router sends to the
   graph, after `expected_fail` drops `3h-002`. The other 14 route to `vector` and
   never reach a selector.

Pre-registering 2 and 3 is what saved the step, for reasons the next section is
entirely about.

## The metric the phase plan specified is nearly uninformative

The plan says selection accuracy is measurable against `ontology_edges`. Measured
**before either arm existed**:

| | value |
|---|---|
| a template traverses a declared edge | 9 of 9 |
| the linker can fill that template | 9 of 9 |
| both hold for the same template | 9 of 9 |
| `always-obligations_for_system` | **8 of 9** |
| `always-obligations_for_role` | 7 of 9 |

Ceiling 9, floor 8. `obligations_for_role` and `obligations_for_system` between
them traverse `APPLIES_TO`, `IMPOSES` and `CLASSIFIED_AS`, which nearly every eval
row declares, so a selector that emits one of them unconditionally scores 89% on
the plan's own metric. That is ADR-0012's finding — a classifier beaten by the
majority-class constant — reproduced at the selection boundary, except this time
it was found before an arm existed rather than after one was believed.

The figure is still published, always beside its constants. Deleting an
uninformative number is how it gets rediscovered as an informative one.

## Decision

**Adopt the deterministic rules.** `template_selector.ADOPTED = "rules"`.

| arm | gold hits | rows with a hit | calls | 0-row calls | cost | p50 |
|---|---|---|---|---|---|---|
| **rules** | **24 of 32** | 8 of 9 | 18 | 2 of 18 | **$0.00** | **5.7 ms** |
| R7B | 14 of 32 | 4 of 9 | 9 | **4 of 9** | $0.000159 | 951 ms |
| *oracle* | *24 of 32* | — | — | — | — | — |

Per stratum, gold hits against gold available:

| stratum | rules | R7B |
|---|---|---|
| aggregation | **13 / 13** | 11 / 13 |
| two-hop | 7 / 10 | 2 / 10 |
| cross-regulation | 3 / 6 | 1 / 6 |
| three-hop | 1 / 3 | 0 / 3 |

The rules arm reaches the oracle exactly. It is permitted up to 3 calls per
question and matched the best *single* call without beating it, so combining
templates bought nothing on this set.

## Why R7B lost, which is not the reason ADR-0012's R7B lost

The router's R7B failed at classification — it never emitted `both` at all. This
one mostly picked sensible templates. **It could not fill them.**

| what R7B produced | rows | the key the graph holds | rows |
|---|---|---|---|
| `system_type=high-risk AI system` | **0** | `high risk ai system` | 169 |
| `article=gdpr` | **0** | `GDPR` | 29 |
| `system_type=narrow procedural task` | **0** | — | — |

**4 of its 9 calls matched no node.** Every one of those calls passes
`graph_query.validate()`, because validation checks parameter *names* and the
graph matches parameter *values*. ADR-0002's boundary is doing precisely its job
and is silent here by construction — which is worth stating plainly, because
"validated" reads like "correct" and it is not. This is the same lesson as *rows
are not correctness*, one layer down: **validation is not correctness either.**

The mapping R7B cannot guess is `normalize()`: lowercased, de-hyphenated, except
where `ABBREVIATIONS` forces `gdpr` → `GDPR` — the exact trap
`docs/metrics/graph-load.md` warned the linker about in Step 2. Reproducing it
from a question is the entity linker's entire job, and the linker does it
deterministically for $0.00.

R7B also emitted two calls mixing parameters from two templates
(`obligations_for_role role=importer system_type=high-risk AI system`). Both were
rejected by `validate()` — the first time that guard has fired on real model
output rather than on the synthetic injection test written for it.

**The arm was not handicapped.** It got a retry policy matching every other call
site (added after the first sweep died on a 429), temperature 0, a fixed seed, six
hand-written few-shot examples none of which is an eval question, and a system
prompt that states the canonical-form rule outright: *"use the plain lowercase
wording the legislation uses, singular, with no article."* It still produced
`high-risk AI system`.

### The model arm is not reproducible

Two sweeps of the same 23 questions at `temperature=0, seed=42` returned **16 then
14** gold hits, with plans differing on several rows — Cohere's `seed` is
best-effort. The rules arm is pinned by
`test_the_rules_arm_still_reproduces_the_artifact`, which re-derives every plan and
compares byte-for-byte. A number that moves between identical runs cannot anchor
an ADR, so the table above quotes the committed artifact and the test asserts a
bound (`r7b <= 20`) rather than an equality. The 10-point gap to 24 is wider than
the observed spread, which is the claim this decision rests on.

## Also decided here: `path_to_prose`'s signature

The Phase 0 stub was `path_to_prose(paths: list[dict]) -> list[ContextDoc]`. With
only rows, it can tell an `obligations_for_role` row from a `definition_of` row
solely by sniffing which keys are present — a guess dressed as a dispatch. Widened
to:

```python
path_to_prose(rows, template, *, labels, max_provenance=MAX_PROVENANCE)
```

`labels` maps `chunk_id -> citation_label` and is **injected**, which keeps the
function pure and testable with no database, and keeps the two-store join in one
place (`graph_path.label_map`). `citation_label` is SELECTed from pgvector and
never recomputed, matching `retriever.py:170`; deriving it here would put two code
paths behind the string Phase 5 grades on.

This is the same treatment Step 0 gave the four signatures it changed (ADR-0011),
recorded so the diff is not read as scope creep.

## Consequences

- The graph path costs **$0.00 and ~6 ms** to plan. All per-query cost on the
  `graph` route is Neo4j and Postgres round trips, not tokens.
- **ADR-0002's first half stands; its second half does not.** "The model chooses a
  template" is viable — R7B's template picks were largely reasonable. "and fills
  parameters" is not, against a graph keyed on normalized names. The security
  argument for the template library is untouched: `validate()` rejected every
  malformed call before a driver opened.
- **Selection is not the binding constraint.** The rules reach the oracle, so the
  8 unreached gold chunks are a template-library limit, not a chooser limit. They
  sit behind `REFERENCES` and `LISTED_IN`, which no typed template traverses.
- **A model arm remains cheap to re-test.** `select_by_model` stays in the module
  behind `ADOPTED`, so a future model can be measured on the committed artifact's
  denominator without rebuilding anything.
- The losing arm's numbers stay here rather than being deleted, per ADR-0012's
  precedent.

## What this does not claim

Gold yield is **provenance coverage, not answer accuracy**. A gold chunk appearing
in a statement's citation list is not the same as the answer being right; Phase 5's
judge is what closes that gap. And every figure here is in-sample on 9 scored rows
— two strata are 2 rows each, so one question moves them 50 points.
