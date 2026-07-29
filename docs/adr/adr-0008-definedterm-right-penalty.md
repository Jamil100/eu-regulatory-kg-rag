# ADR 0008: Add `DefinedTerm`, `Right` and `Penalty` to the ontology, and enforce edge endpoint types

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** Jamil (project owner)
- **Context phase:** Phase 1 — extraction, pre-flight probes before the full-corpus run
- **Supersedes:** the 9-entity / 11-relationship ontology (v2) recorded in
  [`adr-0007-lawfulbasis-permits.md`](adr-0007-lawfulbasis-permits.md)

## Context

Before paying for the full 1,108-chunk extraction, two probes ran against the v2 ontology: three
penalty chunks (to exercise `PENALIZED_UNDER`, the only relationship type with zero uses) and a
seeded random 15-chunk sample drawn at 0.91× the corpus mean length (the existing pilot skews to
2.08×, having been hand-picked for difficulty).

`PENALIZED_UNDER` worked. Reading the rest of the output found three further problems, two of which
are the ADR-0007 failure repeating with different nouns.

### 1. `RiskCategory` had become a dumping ground

Of six distinct `RiskCategory` values across 28 extracted chunks, **only `high-risk` was actually a
risk category**:

| Value | From | Actually is |
|---|---|---|
| `making available on the market` | `aia-art3-def10` | a defined market activity |
| `biometric data` | `aia-art3-def39` | a data category |
| `special categories of personal data` | `aia-art3-def37` | a data category |
| `specific categories of personal data` | `gdpr-art49-para5` | a data category |

The v2 ontology had no home for a term of art, so definienda landed in the nearest wrong slot.
Worse, **the system prompt's own Example 2 taught the mistake**, typing `biometric data` as
`RiskCategory`. AIA Art. 3 has 94 definition chunks and GDPR Art. 4 adds more; left alone this would
have mistyped a large fraction of the corpus and rendered the `RiskCategory` label meaningless — a
query for "what is high-risk" would have returned "making available on the market".

### 2. Rights were modelled two different ways in adjacent paragraphs

| Chunk | v2 output |
|---|---|
| `gdpr-art21-para2` | `Obligation: allow data subjects to object` + `IMPOSES` |
| `gdpr-art21-para5` | `LawfulBasis: right to object by automated means` + `PERMITS` |

The same right, two incompatible representations. Entity resolution could never merge them: it
compares within a type, and these are different types. GDPR Chapter III (Arts. 12–22) is *entirely*
data-subject rights — roughly 80 chunks — and those same rights are what GDPR Art. 83(5)(b) fines
breaches of.

Regulations make four basic moves. v2 could represent three:

| Legal move | v2 representation | Covered? |
|---|---|---|
| You **must** | `IMPOSES` → `Obligation` | yes |
| You **must not** | `CLASSIFIED_AS` prohibited | yes |
| You **may, if** | `PERMITS` → `LawfulBasis` | yes (ADR-0007) |
| You **are entitled to** | — | **no** |

### 3. Penalty amounts were not represented at all

Neither `EUR 15 000 000 / 3 %` (AIA Art. 99(4)) nor `EUR 20 000 000 / 4 %` (GDPR Art. 83(5))
appeared anywhere in the extraction. `PENALIZED_UNDER` records *that* a duty is sanctioned by a
provision, never *how much*. The roadmap's flagship three-hop question — *"which obligations apply,
who enforces them, **and what fines are possible?**"* — was unanswerable from the graph path.

### 4. Head/tail types were documented but unenforced

`Literal` validates the type *string*. It cannot see that an edge's ends are the wrong kind of
thing. Two silent errors passed validation clean:

```
ENFORCED_BY  take necessary steps to comply -> AIA          (tail is a Regulation, not an Authority)
EXEMPT_FROM  AIA Art. 5                     -> AIA Art. 99  (0.90)
```

The second is legally false. "other than those laid down in Article 5" routes Art. 5 breaches to a
*higher* tier (Art. 99(3), EUR 35 000 000 / 7 %); it does not exempt them. The graph asserted that
the AI Act's most severely punished provision carries no penalty.

## Decision

**Three entity types** (9 → 12): `DefinedTerm`, `Right`, `Penalty`.
**Two relationship types** (11 → 13): `GRANTS` (provision → Right), `SETS_PENALTY` (provision → Penalty).

`RiskCategory` is narrowed by prompt to risk gradings only. `DefinedTerm` is declared the default
home for a definiendum. `Penalty` canonical names must carry the magnitude, so the amount survives
into the graph. Example 2 in the system prompt was corrected, and a fifth example added covering a
right and a penalty together.

Three supporting changes, all in `src/ingest/extract.py`:

- **`ALLOWED_ENDPOINTS`** — a head/tail type table for all 13 relationships, checked after parsing by
  `endpoint_violations()`. Violations are **counted and printed, never silently dropped**: a dropped
  edge is indistinguishable from one that was never extracted, which is exactly how the v1 hole hid.
- **`orphan_entities()`** — the mirror of the existing `dangling_refs()`. That function finds edges
  with no entity; nothing found entities with no edge. `aia-art99-para4` had declared nine cited
  Articles and connected none of them, and every check passed.
- **Article sub-numbering** — a chunk's own article must carry its paragraph or definition number
  (`GDPR Art. 13(2)`, not bare `GDPR Art. 13`), taken from chunk metadata. Bare and sub-numbered
  names become separate nodes, severing every cross-reference into that paragraph.

## Consequences

Measured by re-running both probes under v3 (28 chunks total):

| | v2 | v3 |
|---|---|---|
| `RiskCategory` values that are real risk gradings | 1 of 6 | **1 of 1** |
| `gdpr-art21-para2` / `-para5` (same right) | `Obligation` / `LawfulBasis` | **`Right` + `GRANTS` in both** |
| Penalty amounts captured | 0 | **2 of 2, magnitude intact** |
| False `EXEMPT_FROM` on `aia-art99-para4` | 2 | **0** |
| Bare self-article names | ~50% | **0 of 27** |
| `REFERENCES` edges across the sample | 12 | **50** |
| `aia-art9-para1` control (a genuine duty) | 4 `Obligation` + `IMPOSES` | **unchanged** |

The control matters as much as the fixes: the three new types did not bleed into real obligations.

**Cost.** The v3 system prompt is ~2,040 tokens longer and the model emits more per chunk, so the
full-corpus estimate moves from **$14.43 to $23.13** (+60%) on a representative sample. Input is now
65% of the run cost and ~96% of input is the fixed system prompt.

That input cost is structural. Prompt caching would have addressed it, but **Command A does not
support it on Cohere's API** (verified 2026-07-29: no `cache_control`-style parameter in the v2 Chat
API, no cached-token line in pricing, nothing in the changelog through the Command A+ release in May
2026). Batching chunks per call was already declined because it muddies `source_chunk_id`. The only
remaining lever is prompt length — the five few-shot examples are ~2,530 tokens, 47% of the prompt
and $7.01 of the run — and it is a lever worth ~$2.25 in practice against a real risk of regressing
the constructs this ADR exists to fix. **The +$8.70 is accepted as the price of the ontology.**

**Accepted regressions and limits:**

- `gdpr-art83-para5` gained its `Penalty` but lost 6 `PENALIZED_UNDER` edges (7 → 0), while the AIA
  equivalent kept both. Penalty-chunk handling is not yet consistent across the two regulations.
- Orphan entities rose (11 across 13 chunks), mostly `DefinedTerm`s the model names without
  connecting. Harmless as disconnected nodes, and now measured rather than invisible.
- Endpoint violations still occur at roughly 1 per 13 chunks. They are detected, not prevented.
  Detection was chosen over auto-repair for the reason given above.
- `Right` is now consistently typed but not consistently *named* (`right to object` vs `right to
  object to direct marketing`). That is entity resolution's job (Phase 1, Step 3) — and it is only
  tractable because both are now the same type.
