"""The `Chunk` model against the corpus it claims to describe.

This test exists because the interface between `src/schemas.py` and the chunker
output was never crossed by anything. `chunker.py` writes raw dicts via
`json.dumps` and no code had ever constructed a `Chunk` from a real row, so a
declared `article: str | None` sat against an int-writing chunker until it was
noticed by hand while planning Phase 2 -- rejecting 1,000 of 1,108 rows.

So the assertion that matters is the boring one: **all 1,108 rows, both files,
every field**. Anything narrower reproduces the original mistake, which was
checking one part and assuming it covered the whole.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.ingest.extract import CHUNK_FILES, ROOT
from src.schemas import Chunk

# Counted from the corpus, asserted rather than described: 906 paragraph rows,
# 108 annex rows, 94 definition rows.
EXPECTED_TOTAL = 1108
EXPECTED_SHAPES = {"paragraph": 906, "annex": 108, "definition": 94}

# Per-annex counts, previously prose in docs/failure-notes.md. A fused annex is
# invisible to a total: the 102-vs-108 collapse that this table is here to catch
# was found by comparing per-annex numbers, and a coverage check ("every container
# yields >= 1 chunk") would have passed it.
#
# Honest limitation: these are transcribed from the current corpus, not from an
# independent hand count. So this guards against a *regression* from today's
# verified output; it does not re-verify the parser against the source HTML.
EXPECTED_ANNEX_COUNTS = {
    "I": 20, "II": 1, "III": 8, "IV": 9, "V": 8, "VI": 4, "VII": 5,
    "VIII": 27, "IX": 5, "X": 7, "XI": 5, "XII": 2, "XIII": 7,
}


@pytest.fixture(scope="module")
def raw_rows() -> list[dict]:
    rows = []
    for path in CHUNK_FILES:
        with path.open(encoding="utf-8") as fh:
            rows.extend(json.loads(line) for line in fh if line.strip())
    return rows


@pytest.fixture(scope="module")
def chunks(raw_rows) -> list[Chunk]:
    return [Chunk.model_validate(row) for row in raw_rows]


def test_every_row_validates(raw_rows):
    """The whole corpus, not a sample. Reports which rows failed, not just how many."""
    failures = []
    for row in raw_rows:
        try:
            Chunk.model_validate(row)
        except ValidationError as exc:
            failures.append((row.get("chunk_id"), str(exc).splitlines()[1:3]))

    assert not failures, f"{len(failures)} of {len(raw_rows)} rows rejected: {failures[:5]}"
    assert len(raw_rows) == EXPECTED_TOTAL


def test_no_key_is_silently_dropped(raw_rows):
    """`extra="forbid"` is the guard; this proves it is actually load-bearing.

    The old model "accepted" annex rows by discarding `annex`, `annex_title`,
    `point` and `token_count`. A round-trip comparison catches that class of
    acceptance, where a plain validate-and-count does not.
    """
    for row in raw_rows:
        dumped = Chunk.model_validate(row).model_dump(exclude_none=True)
        assert dumped == row, f"{row['chunk_id']}: round-trip lost or changed keys"


def test_shape_counts(chunks):
    counts: dict[str, int] = {}
    for chunk in chunks:
        counts[chunk.shape] = counts.get(chunk.shape, 0) + 1
    assert counts == EXPECTED_SHAPES


def test_per_annex_counts(chunks):
    """A silent collapse or explosion in one annex breaks the build.

    Cross-checked against EXPECTED_SHAPES so the two tables cannot drift apart:
    if someone updates one number here they must account for the total too.
    """
    counts: dict[str, int] = {}
    for chunk in chunks:
        if chunk.annex is not None:
            counts[chunk.annex] = counts.get(chunk.annex, 0) + 1
    assert counts == EXPECTED_ANNEX_COUNTS
    assert sum(EXPECTED_ANNEX_COUNTS.values()) == EXPECTED_SHAPES["annex"]


def test_shape_rejects_an_unknown_row():
    with pytest.raises(ValidationError):
        Chunk(chunk_id="x", regulation="AIA", text="...", article=9)


def test_extra_key_is_rejected():
    with pytest.raises(ValidationError):
        Chunk(
            chunk_id="aia-art9-para1", regulation="AIA", text="...",
            article=9, article_title="Risk management system", paragraph=1,
            unexpected_new_chunker_field="surprise",
        )


def test_citation_labels_match_the_eval_set_format(chunks):
    """The gold `citations` strings must be reproducible from a Chunk alone.

    Sampled against eval/eval-questions.jsonl, which writes `AIA Art. 9(2)` and
    `AIA Annex III(4)`. If these drift apart, a generated citation can never be
    string-compared to a gold one.
    """
    by_id = {c.chunk_id: c for c in chunks}
    assert by_id["aia-art9-para2"].citation_label == "AIA Art. 9(2)"
    assert by_id["aia-art3-def37"].citation_label == "AIA Art. 3(37)"
    assert by_id["aia-annex3-point4"].citation_label == "AIA Annex III(4)"
    assert by_id["gdpr-art83-para5"].citation_label == "GDPR Art. 83(5)"


def test_sectioned_annexes_keep_their_section(chunks):
    """Annexes VIII and XI restart point numbering per section.

    The section was folded into the chunk_id and dropped from the record, so
    `Annex VIII(1)` named three different provisions to anything reading the
    fields. 32 rows carry a section; these are the ones that used to collide.
    """
    by_id = {c.chunk_id: c for c in chunks}
    assert by_id["aia-annex8-sectionA-point1"].citation_label == "AIA Annex VIII(A)(1)"
    assert by_id["aia-annex8-sectionC-point1"].citation_label == "AIA Annex VIII(C)(1)"
    assert by_id["aia-annex11-section2-point1"].citation_label == "AIA Annex XI(2)(1)"
    assert sum(1 for c in chunks if c.section) == 32
    # Every other annex numbers straight through and must stay unsectioned.
    assert by_id["aia-annex3-point1"].section is None


def test_chunker_writes_the_files_everything_else_reads(chunks):
    """`chunker.output_path()` must reproduce the two hardcoded corpus paths.

    The chunker used to write one hardcoded `chunks.jsonl` that was renamed by
    hand after each run. Deriving the name is only safe if it derives the *same*
    name: `extract.py`'s CHUNK_FILES, the embedder and these fixtures all hardcode
    `chunks-ai-act.jsonl` / `chunks-gdpr.jsonl`.
    """
    from src.ingest.chunker import output_path

    assert {output_path("AIA"), output_path("GDPR")} == set(CHUNK_FILES)


def test_chunker_reproduces_the_stored_corpus(tmp_path, monkeypatch):
    """`main()` end-to-end must rebuild both files byte-for-byte.

    It did not. `main()` called `chunk()` and `chunk_annexes()` but never
    `chunk_definitions()`, so AIA Art. 3 came out as one 2,619-word blob instead
    of 68 definition chunks (GDPR Art. 4: one instead of 26). The corpus on disk
    had 1,108 rows; the documented command sequence rebuilt 1,016 of them.

    Every other test in this file reads the *stored* corpus, so all of them passed
    against a corpus the code could no longer produce. Comparing the artefact to
    its own generator is the only check that catches that.
    """
    from src.ingest import chunker

    monkeypatch.setattr(chunker, "PROCESSED_DIR", tmp_path)
    for source in ("data/eu-ai-act.html", "data/gdpr.html"):
        path = ROOT / source
        if not path.exists():
            pytest.skip(f"{source} not present; source HTML is needed to re-derive")
        assert chunker.main([str(path)]) == 0

    for stored in CHUNK_FILES:
        rebuilt = tmp_path / stored.name
        assert rebuilt.read_bytes() == stored.read_bytes(), (
            f"{stored.name}: re-deriving from source HTML does not reproduce the corpus"
        )


def test_citation_labels_are_unique(chunks):
    """One locator per chunk, or citation validation cannot resolve back to text."""
    labels = [c.citation_label for c in chunks]
    assert len(set(labels)) == len(labels)
