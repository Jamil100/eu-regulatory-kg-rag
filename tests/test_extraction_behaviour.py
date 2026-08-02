"""What the extractor *decided*, not whether its output parses.

Every existing check on `extractions.jsonl` is structural: does it validate, are
the endpoints legal, are there dangling refs. All of those pass on an extraction
that is well-formed and wrong. The distinction ADR-0007 is about -- a provision
that *permits* something is not an `Obligation` -- was verified once by hand
during the ontology v3 re-run and left nothing behind that would catch a
regression. This is that check, made permanent.

Runs off `data/processed/extractions.jsonl`, which is already on disk, so it
costs nothing and calls no API. Skips rather than fails when the file is absent,
following the convention in `conftest.py`: a suite that goes red on missing
inputs teaches people to ignore it.
"""

from __future__ import annotations

import json

import pytest

from src.ingest.extract import EXTRACTIONS_PATH
from src.schemas import Extraction


@pytest.fixture(scope="module")
def extractions() -> dict[str, Extraction]:
    if not EXTRACTIONS_PATH.exists():
        pytest.skip(f"{EXTRACTIONS_PATH.name} not found; run `python -m src.ingest.extract --all`")
    rows = {}
    with EXTRACTIONS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = Extraction.model_validate(json.loads(line))
                rows[row.chunk_id] = row
    return rows


def _entity_types(extraction: Extraction) -> list[str]:
    return [entity.type for entity in extraction.entities]


def _require(extractions: dict[str, Extraction], chunk_id: str) -> Extraction:
    if chunk_id not in extractions:
        pytest.skip(f"{chunk_id} is not in the extraction output")
    return extractions[chunk_id]


def test_a_permission_yields_no_obligation(extractions):
    """GDPR Art. 6(1) lists the six lawful bases. It permits; it does not command.

    Reading it as a duty was the original ontology error (ADR-0007) -- it produced
    `Obligation` nodes for "consent of the data subject", which then answered
    obligation queries with things nobody is obliged to do.
    """
    extraction = _require(extractions, "gdpr-art6-para1")
    types = _entity_types(extraction)

    assert "Obligation" not in types, (
        f"gdpr-art6-para1 yielded {types.count('Obligation')} Obligation entities; "
        f"Art. 6(1) grants lawful bases, it imposes no duty. Got: {types}"
    )
    # The positive half: it must still extract *something*, and the right thing.
    # Without this, deleting the LawfulBasis type entirely would pass the above.
    assert "LawfulBasis" in types, f"no LawfulBasis found in gdpr-art6-para1: {types}"


def test_a_genuine_duty_still_yields_an_obligation(extractions):
    """The control for the test above.

    AIA Art. 9(1) is a real duty ("shall be established, implemented, documented
    and maintained"). Without this assertion, an extractor that emitted no
    `Obligation` anywhere in the corpus would pass the permission test cleanly --
    which is exactly the shape of false pass the failure notes are about.
    """
    extraction = _require(extractions, "aia-art9-para1")
    types = _entity_types(extraction)

    assert "Obligation" in types, (
        f"aia-art9-para1 yielded no Obligation; it is the risk-management duty. Got: {types}"
    )
