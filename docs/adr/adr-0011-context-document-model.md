# ADR 0011: A `ContextDoc` model for the query/answer path, not a wider `Chunk`

- **Status:** Accepted
- **Date:** 2026-08-02
- **Deciders:** Jamil (project owner)
- **Context phase:** Phase 3 — Step 0, before any query/answer code is written

## Context

`src/query/` and `src/answer/` were stubbed out in Phase 0, before the graph, the vector index, or
the corpus existed. Three of the stub signatures declare `Chunk` as their return or parameter type,
and `Chunk` cannot honour what they need:

- `retrieve(question, top_k=50) -> list[Chunk]` (`src/query/retriever.py`) needs a similarity score
  attached to each result — the reranker has nothing to improve on otherwise.
- `rerank(question, candidates: list[Chunk], top_n=5) -> list[Chunk]` (`src/query/reranker.py`)
  needs the same score field, going in and coming out changed.
- `path_to_prose(paths: list[dict]) -> list[Chunk]` (`src/answer/path_to_prose.py`) is the sharpest
  case: its own docstring says "each statement keeps its `source_chunk_id`", but a rendered graph
  statement — *"Deployers of high-risk systems must conduct a fundamental rights impact assessment
  (AI Act, Article 27)"* — is not a row from `chunks-ai-act.jsonl`. It may be built from more than
  one asserted or derived relationship (ADR-0010), and `Chunk` has no field for either the
  provenance or the `derived` flag.
- `assemble(graph_docs: list[Chunk], passage_docs: list[Chunk]) -> list[dict]`
  (`src/answer/context_assembly.py`) inherits the same conflict on its `graph_docs` side.

`Chunk` is `model_config = ConfigDict(extra="forbid")` and this is deliberate, not an oversight to
work around. Its own docstring records why: a previous version declared `article: str | None`
against a chunker that writes ints, and because Pydantic v2 does not coerce `int` → `str`,
`Chunk.model_validate` rejected **1,000 of the 1,108 rows**. The 108 that "passed" did so only by
silently discarding `annex`, `annex_title`, `point`, and `token_count` — the exact fields that
identified them. `extra="forbid"` exists so a shape mismatch fails loudly instead of vanishing.

Two options:

1. **Loosen `Chunk`** — add `score: float | None = None`, `source: Literal[...] | None = None`,
   drop `extra="forbid"`, or make the shape-check optional. Fixes the immediate signatures with the
   smallest diff.
2. **Add a separate model** for a document on its way into a prompt, and widen the four stub
   signatures to use it instead.

## Decision

**Add `ContextDoc`, leave `Chunk` untouched.**

```python
class ContextDoc(BaseModel):
    chunk_id: str
    text: str
    citation_label: str
    source: Literal["GRAPH", "PASSAGE"]
    score: float | None = None
    derived: bool = False
```

`Chunk` describes a row that exists in the corpus, independent of any question ever being asked of
it — that is what `extra="forbid"` protects and what 1,108 rows of tests assert. `ContextDoc`
describes something manufactured for one request: a retrieved passage with a score attached, or a
graph statement rendered from one or more relationships, neither of which is "a chunk with an extra
field." Loosening `Chunk` to fit both would have repeated the shape it already failed once in the
opposite direction — accepting a wider range of inputs by growing optional fields until a mismatch
stops being visible. `extra="forbid"` only protects against fields the model doesn't expect; it does
nothing once every field is `| None`.

`chunk_id` and `citation_label` are required on every `ContextDoc`, including `source="GRAPH"` rows.
A rendered graph statement is provenance-bearing prose built from one or more chunks, not a
free-floating fact — `path_to_prose` is responsible for keeping that true, by construction, rather
than `ContextDoc` making the fields optional and hoping every caller fills them in.

Four stub signatures changed to use it:

| File | Before | After |
|---|---|---|
| `src/query/retriever.py` | `retrieve(...) -> list[Chunk]` | `-> list[ContextDoc]` |
| `src/query/reranker.py` | `rerank(..., candidates: list[Chunk]) -> list[Chunk]` | `list[ContextDoc]` both sides |
| `src/answer/path_to_prose.py` | `path_to_prose(...) -> list[Chunk]` | `-> list[ContextDoc]` |
| `src/answer/context_assembly.py` | `assemble(graph_docs: list[Chunk], passage_docs: list[Chunk])` | both `list[ContextDoc]` |

None of the four function bodies were implemented as part of this change — they still raise
`NotImplementedError`. This ADR resolves the type conflict the Phase 3 plan's Step 0 named; it is not
Steps 4–6, which fill in what these functions actually do.

`assemble`'s return type stays `list[dict]`. That is the boundary where a typed `ContextDoc` becomes
an untyped document for Cohere's `documents` parameter, and blurring it — returning a Pydantic model
on one side of a Cohere API call and a plain dict on the other — would hide where the API's own
contract, not this codebase's, takes over.

## Consequences

- A corpus row and a prompt-bound document are now different types, so a future change to one
  cannot silently change the other's validation behaviour.
- Every consumer of `retrieve`, `rerank`, `path_to_prose`, or `assemble` must import `ContextDoc`
  from `src.schemas` rather than `Chunk` — a one-time cost paid now, before Steps 4–6 write against
  the old signatures and have to be revisited.
- `ContextDoc.score` is `float | None` rather than required, because a graph-derived statement has
  no natural similarity score; a consumer must not assume every `ContextDoc` is rankable by score
  without checking which path produced it.
- The `derived` flag is carried at the `ContextDoc` level, not just on the underlying Neo4j edge, so
  a citation-validation or generation step that never touches the graph directly can still see it.
