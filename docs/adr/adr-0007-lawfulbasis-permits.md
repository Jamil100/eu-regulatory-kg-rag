# ADR 0007: Add `LawfulBasis` entity and `PERMITS` relationship to the ontology

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** Jamil (project owner)
- **Context phase:** Phase 1 — extraction, after the 10-chunk test run
- **Supersedes:** the 8-entity / 10-relationship ontology recorded in `docs/concepts/ontology.md` (v1)

## Context

The ontology started with 8 entity types and 10 relationship types, designed by reading a sample of
real articles (AIA Art. 3, 8, 9, 26, 43, 60, 74; GDPR Art. 9). Every relationship type expressed
either a **duty** (`IMPOSES`) or a **prohibition/constraint** (`CLASSIFIED_AS` prohibited,
`EXEMPT_FROM`, `ENFORCED_BY`). None expressed **permission**.

This gap was invisible until the 10-chunk extraction test. Running Command A over `gdpr-art6-para1`
produced six `Obligation` entities wired to `GDPR Art. 6(1)` by `IMPOSES` at 0.95 confidence:

```
IMPOSES  GDPR Art. 6(1) -> obtain consent               (0.95)
IMPOSES  GDPR Art. 6(1) -> perform contract             (0.95)
... (four more)
```

This is legally false. Article 6(1) imposes no duties — it states that processing is lawful **if at
least one** of the six bases applies. They are permissive, alternative conditions, not mandatory
obligations. The graph as extracted would answer *"what must a controller do under Article 6?"* with
six obligations that do not exist.

The same distortion appeared in `gdpr-art9-para2` (the (a)–(j) derogations became 13 phantom
obligations) and produced spurious `ENFORCED_BY` edges on "Union or Member State law." All of it
traces to one root cause: **the ontology could not represent permission.**

Regulations make three basic moves. The v1 ontology could represent only two:

| Legal move | v1 representation | Covered? |
|---|---|---|
| You **must** | `IMPOSES` → `Obligation` | yes |
| You **must not** | `CLASSIFIED_AS` prohibited | yes |
| You **may, if** | — | **no** |

GDPR Article 6 consists entirely of the third move, so it had nowhere valid to land.

Critically, this was not caught by validation. The extraction had a **0% Pydantic failure rate** —
every record was schema-valid. The schema was satisfied; the facts were still wrong. Schema validity
measures whether the model filled the shape, not whether the shape can represent the domain.

## Decision

Add one entity type and one relationship type to the ontology:

- **`LawfulBasis`** (9th entity type) — a ground that makes an activity lawful or lifts a
  prohibition: consent, contract, legal obligation, vital interests, public task, legitimate
  interests (GDPR Art. 6), and the Art. 9(2) derogations / special-category exceptions. Explicitly
  **not** an `Obligation` — it permits, it does not require.

- **`PERMITS`** (11th relationship type) — the permissive edge: *head makes tail lawful / justified
  / allowed*. Used as `Article PERMITS LawfulBasis` and `LawfulBasis PERMITS <activity>`. It is the
  counterpart of `IMPOSES`: `IMPOSES` asserts a duty, `PERMITS` asserts an allowance.

Both are enforced via the `Literal` types in the extraction schema. The extraction system prompt
gains an explicit disambiguation rule ("if the text says an activity is lawful/permitted when a
condition holds, model the condition as `LawfulBasis` + `PERMITS`, never `Obligation` + `IMPOSES`")
and a few-shot example built from GDPR Art. 6(1).

The fix was validated by re-testing the four affected chunks plus a **control chunk**
(`aia-art9-para1`, a genuine obligation) to confirm the new permissive rule fixes the false
obligations without bleeding into real duties.

## Why the entity alone was not sufficient

Adding `LawfulBasis` fixes *what the six things are*, but they still need an edge to Article 6, and
every existing relationship type is wrong for permission:

- `IMPOSES` — asserts a duty (false)
- `EXEMPT_FROM` — about escaping a prohibition (wrong direction and meaning)
- `APPLIES_TO` — loses the permissive meaning entirely

There was no permissive edge anywhere in the ontology. So the gap required both a noun
(`LawfulBasis`) and a verb (`PERMITS`) — one gap, two pieces to fill it.

## Consequences

**Positive**

- GDPR Art. 6 and the Art. 9(2) derogations now model correctly; queries about controller duties no
  longer return invented obligations.
- The spurious `ENFORCED_BY` strain on "Union/Member State law" resolves — it was the same
  permissive-vs-mandatory confusion.
- The ontology can now represent all three basic regulatory moves (must / must not / may-if),
  making it robust for the GDPR half of the corpus, which is far more permission-oriented than the
  AI Act.

**Negative / accepted cost**

- Two more types for the extractor to reason about (minor; the disambiguation rule and control-chunk
  test mitigate the risk of misclassification).
- The v1 test results for permission-bearing chunks are invalidated and must be re-extracted (small
  — only the affected chunks, cache-busted).

**Known limitation left unsolved (deliberately)**

- `PERMITS` records *that* a basis makes something lawful, but not that the bases are **alternatives**
  ("at least one of"). The graph shows six `PERMITS` edges without encoding that satisfying *one*
  suffices. Modeling n-ary "one-of" constraints in a property graph is a research-grade problem
  beyond this project's scope. It is flagged in `docs/failure-notes.md` rather than solved, and the
  vector retrieval path still carries the exact disjunctive wording for any query that needs it.

## Alternatives considered

- **Add `LawfulBasis` only, reuse an existing edge.** Rejected — no existing edge expresses
  permission; reusing one would re-encode the same false-obligation or wrong-direction error.
- **Do nothing, accept the distortion.** Rejected — it produces confidently false answers on a
  whole class of GDPR questions, including eval items, at 0.95 confidence.
- **Model lawful bases as `Obligation` with a `polarity: permissive` property instead of a new
  type.** Rejected — it hides a fundamental type distinction inside a property, so graph traversals
  and Cypher templates would have to special-case it everywhere; a first-class type is cleaner and
  self-documenting.

## Lesson captured

A 0% validation-failure rate looked like success and wasn't. **Validity is not correctness.** The
distinction — and catching it by reading outputs on a small, varied sample rather than trusting an
aggregate metric — is the transferable takeaway, and the reason the extraction pipeline tests on 10
chunks before committing to the full corpus.
