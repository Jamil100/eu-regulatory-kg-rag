"""Shared Pydantic models and the ontology: the single source of truth.

The ontology (12 entity types, 13 relationship types) used to live in
`src/ingest/extract.py` and be re-exported here. That made `src/api/app.py`
import `cohere` transitively just to name an `AskRequest`, so the direction is
now inverted: this module owns the definitions and `extract.py` imports them.
Nothing here may import from `src.ingest`.

`extract.py` re-exports the same names, so `from src.ingest.extract import
Extraction` keeps working for the ingest-side modules that already read that way.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "EntityType",
    "RelationType",
    "ENTITY_TYPES",
    "RELATION_TYPES",
    "ALLOWED_ENDPOINTS",
    "Entity",
    "Relationship",
    "Extraction",
    "Chunk",
    "ChunkShape",
    "ContextDoc",
    "Route",
    "ROUTES",
    "Citation",
    "AskRequest",
    "AskResponse",
]

# --------------------------------------------------------------------------
# Ontology -- locked by Literal, so anything the model invents fails validation
# rather than silently entering the graph.
# --------------------------------------------------------------------------

EntityType = Literal[
    "Regulation",
    "Article",
    "Annex",
    "ActorRole",
    "Obligation",
    "RiskCategory",
    "SystemType",
    "Authority",
    "LawfulBasis",
    "DefinedTerm",
    "Right",
    "Penalty",
]

RelationType = Literal[
    "DEFINED_IN",
    "IMPOSES",
    "APPLIES_TO",
    "CLASSIFIED_AS",
    "LISTED_IN",
    "REFERENCES",
    "ENFORCED_BY",
    "PENALIZED_UNDER",
    "EXEMPT_FROM",
    "INTERACTS_WITH",
    "PERMITS",
    "GRANTS",
    "SETS_PENALTY",
]

# Neo4j 5 cannot parameterize a label or a relationship type, so both are
# interpolated into the query text by `graph_writer`. These frozensets are what
# every value is checked against before it reaches a format string -- nothing
# from the data can become Cypher. Derived from the Literals so the two cannot
# drift apart.
ENTITY_TYPES: frozenset[str] = frozenset(get_args(EntityType))
RELATION_TYPES: frozenset[str] = frozenset(get_args(RelationType))

# Which entity types may sit at each end of each relationship. The prompt states
# these too, but Literal only validates the type *string* -- it cannot see that
# ENFORCED_BY was pointed at a Regulation instead of an Authority. Checked after
# parsing so the violations are counted rather than silently entering the graph.
_PROVISION = {"Article", "Annex"}
_DEFINABLE = {
    "DefinedTerm", "ActorRole", "SystemType", "RiskCategory",
    "Obligation", "Right", "LawfulBasis", "Authority", "Penalty",
}
_PARTY = {"ActorRole", "SystemType", "Authority"}

ALLOWED_ENDPOINTS: dict[str, tuple[set[str], set[str]]] = {
    "DEFINED_IN": (_DEFINABLE, _PROVISION),
    "IMPOSES": (_PROVISION | {"Regulation"}, {"Obligation"}),
    # Deliberately wide on both ends: a duty, a provision, a classification, a
    # basis, a right or a system type can all "govern" a party -- and a duty can
    # equally concern a kind of data ("...applies to personal data"). The
    # obligations_for_role template filters on the :ActorRole label anyway, so a
    # wider tail adds no noise to the query it exists to serve.
    "APPLIES_TO": ({"Obligation", "Article", "Annex", "RiskCategory", "LawfulBasis",
                    "Right", "SystemType", "DefinedTerm"}, _PARTY | {"DefinedTerm"}),
    "CLASSIFIED_AS": ({"SystemType", "DefinedTerm", "ActorRole"}, {"RiskCategory"}),
    "LISTED_IN": ({"SystemType", "DefinedTerm", "Regulation", "Obligation", "Authority"},
                  _PROVISION),
    "REFERENCES": (_PROVISION, _PROVISION),
    # The tight ones -- these are where the probe found real errors.
    "ENFORCED_BY": ({"Obligation", "Regulation", "Article", "Right"}, {"Authority"}),
    "PENALIZED_UNDER": ({"Obligation"}, _PROVISION),
    "EXEMPT_FROM": ({"ActorRole", "SystemType", "Obligation"}, {"Obligation"} | _PROVISION),
    # Annex is a head here because an annex genuinely does interact with a foreign
    # instrument -- AIA Annex VIII points at GDPR Art. 35. Widened 2026-07-31: the
    # old {Regulation, Article} head flagged 11 real Annex->Regulation edges as
    # violations, and would have flagged the derived Annex->Article bridges too.
    # Validation-only, so this costs no re-extraction.
    "INTERACTS_WITH": (_PROVISION | {"Regulation"}, {"Regulation", "Article"}),
    "PERMITS": ({"Article", "Regulation", "Annex"}, {"LawfulBasis"}),
    "GRANTS": (_PROVISION | {"Regulation"}, {"Right"}),
    "SETS_PENALTY": (_PROVISION, {"Penalty"}),
}


class Entity(BaseModel):
    type: EntityType
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)


class Relationship(BaseModel):
    type: RelationType
    head: str
    tail: str
    source_chunk_id: str
    confidence: float = Field(ge=0.0, le=1.0)


class Extraction(BaseModel):
    chunk_id: str
    entities: list[Entity]
    relationships: list[Relationship]


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------

ChunkShape = Literal["paragraph", "annex", "definition"]


class Chunk(BaseModel):
    """One chunk as the chunker actually writes it.

    Every field type here was read off the corpus, not guessed. The previous
    version declared `article: str | None` against a chunker that writes ints,
    and Pydantic v2 does not coerce int->str, so `Chunk.model_validate` rejected
    **1,000 of the 1,108 rows**. The 108 that passed were the annex chunks, and
    they passed only because `annex`, `annex_title`, `point` and `token_count`
    were silently dropped as extra keys -- the model "accepted" them by throwing
    away exactly the fields that identify them.

    `extra="forbid"` is the guard against a repeat: a new chunker key now fails
    loudly here instead of vanishing. `tests/test_chunks.py` round-trips all
    1,108 rows, because an interface between two components is untested until
    something has actually crossed it.
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    regulation: str
    text: str

    # Paragraph and definition rows.
    article: int | None = None
    article_title: str | None = None
    paragraph: int | None = None
    definition: int | None = None

    # Annex rows.
    annex: str | None = None       # roman numeral, e.g. "III"
    annex_title: str | None = None
    section: str | None = None     # only Annexes VIII ("A"/"B"/"C") and XI ("1"/"2")
    point: int | None = None

    token_count: int | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> Chunk:
        """Reject any row that is not exactly one of the three known shapes.

        The shape is what the citation formatter and the SQL columns both key
        off, so it is derived and validated once here rather than re-sniffed at
        each site.
        """
        self.shape  # noqa: B018 -- raises if the row matches no shape
        return self

    @property
    def shape(self) -> ChunkShape:
        if self.annex is not None and self.point is not None:
            return "annex"
        if self.article is not None and self.definition is not None:
            return "definition"
        if self.article is not None and self.paragraph is not None:
            return "paragraph"
        raise ValueError(
            f"{self.chunk_id!r} matches none of the three chunk shapes "
            "(article+paragraph, article+definition, annex+point)"
        )

    @property
    def citation_label(self) -> str:
        """The human-facing locator, in the form the eval set already uses.

        `AIA Art. 9(2)`, `AIA Art. 3(12)`, `AIA Annex III(4)` -- matching the
        `citations` field of eval/eval-questions.jsonl exactly, so a generated
        citation and a gold citation are comparable as strings.

        Sectioned annexes nest the section the way the eval set already nests a
        sub-point (`AIA Annex III(1)(a)`), giving `AIA Annex VIII(A)(1)`. Without
        it, Annexes VIII and XI restart their point numbering per section and 25
        chunks share 11 labels -- `Annex VIII(1)` would name the registration
        duties of three different actors at once.
        """
        match self.shape:
            case "annex":
                section = f"({self.section})" if self.section else ""
                return f"{self.regulation} Annex {self.annex}{section}({self.point})"
            case "definition":
                return f"{self.regulation} Art. {self.article}({self.definition})"
            case _:
                return f"{self.regulation} Art. {self.article}({self.paragraph})"


# --------------------------------------------------------------------------
# Query/answer path (Phase 3) -- a document on its way into a prompt, not a
# corpus row. See docs/adr/adr-0011-context-document-model.md for why this is
# not a wider Chunk: Chunk is extra="forbid" because a previous version of it
# silently accepted 108 malformed rows by dropping the fields that identified
# them, and a retrieval score or a rendered graph statement is not a field a
# corpus chunk can grow without repeating that mistake in the other direction.
# --------------------------------------------------------------------------


class ContextDoc(BaseModel):
    """One retrieved-or-derived document, from either path, ready to be scored,
    reranked, deduped, and handed to Command A's `documents` parameter.

    `chunk_id` and `citation_label` are always real corpus identifiers, even for
    a GRAPH document -- a rendered graph statement is provenance-bearing prose
    built from one or more chunks, not a free-floating fact, and `path_to_prose`
    is responsible for keeping that true.

    `provenance` was added by Step 6 (ADR-0014) and is the widening that makes
    the graph path's citations honest. A statement built from 124 asserting
    chunks previously reached the answer path as `chunk_id=chunks[0]` -- the
    lexicographically smallest of them -- with the rest surviving only as label
    text inside `text` ("... (AIA Art. 26(1), +121 more)"). ADR-0013's headline
    24-of-32 was computed against the full provenance union, which is a number
    `Citation` could never carry: the field the key was derived from was dropped
    at this boundary, which is the recurrence tracker's own row about itself.
    `provenance` is **what was rendered**, so it is at most `max_provenance`
    entries and its first element is always `chunk_id`. It is deliberately not
    the full union: citing 124 chunks for one sentence is the 24,428-row
    multiplication in a new costume (`path_to_prose.py:9-18`).

    `[]` on a PASSAGE document, because a corpus chunk asserts itself and a
    one-element list saying so would invite a caller to treat the two sources
    symmetrically. `provenance == []` is how a consumer tells them apart without
    reading `source`.
    """

    chunk_id: str
    text: str
    citation_label: str
    source: Literal["GRAPH", "PASSAGE"]
    score: float | None = None
    derived: bool = False  # ADR-0010: built from an inferred edge, not an asserted one
    provenance: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# API contracts
# --------------------------------------------------------------------------

# The router's output enum. It lives here rather than in `src/query/router.py`
# because `AskResponse` has to name the same three values and previously spelled
# them out a second time, with nothing pinning the two lists to each other. A
# route added to the router and not to the response -- or the reverse -- would
# have typechecked. `router.py` re-exports this name; the dependency runs that
# way round because nothing in `src.schemas` may import from `src.query`, for the
# same reason the module docstring gives for `src.ingest`.
Route = Literal["graph", "vector", "both"]
ROUTES: frozenset[str] = frozenset(get_args(Route))


class Citation(BaseModel):
    """One span of the generated answer, attributed to one corpus chunk.

    `start`/`end` index **the answer string** and not any single content block.
    Command A returns `content` as a list of blocks and reports offsets into
    `content[content_index]`; `generate()` rebases them, because these two fields
    are declared here as answer offsets and a caller checking
    `answer[start:end] == text` is entitled to have that hold. It is the defect
    `citation_validator.span_defects` exists to catch.

    The three fields Step 6 added (ADR-0014) were all in hand at construction and
    were previously thrown away:

    * `citation_label` -- the string `eval/eval-questions.jsonl` grades on and the
      only part of a citation a reader can check against the Official Journal.
      Reconstructing it downstream would mean a second code path producing the
      string Phase 5 grades, which is the failure `retriever.py:170` and
      `graph_path.py:15-20` both refuse.
    * `source` -- a GRAPH citation is a rendered statement's provenance and a
      PASSAGE citation is the statute text the model read. Both are legitimate
      and they are not the same kind of evidence.
    * `document_id` -- which assembled document (`d0..dN`) the model actually
      cited. One document fans out to up to `MAX_PROVENANCE` citations sharing
      one span, so without this the fan-out is unattributable and a duplicate id
      collapsing the reverse map is invisible.
    """

    chunk_id: str
    start: int
    end: int
    text: str
    citation_label: str = ""
    source: Literal["GRAPH", "PASSAGE"] = "PASSAGE"
    document_id: str = ""


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    route: Route
    latency_ms: float
    cost_usd: float
