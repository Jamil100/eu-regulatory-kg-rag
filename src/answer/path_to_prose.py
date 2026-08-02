"""Convert graph paths into readable statements before they reach the prompt.

e.g. (deployer)-[APPLIES_TO]-(FRIA obligation)-[IMPOSED_BY]-(AIA Art. 27)
  -> "Deployers of high-risk systems must conduct a fundamental rights impact
      assessment (AI Act, Article 27)."
Each statement keeps its source_chunk_id.
"""

from __future__ import annotations

from src.schemas import ContextDoc


def path_to_prose(paths: list[dict]) -> list[ContextDoc]:
    """Render Neo4j paths as prose statements carrying source_chunk_id.

    Returns ContextDoc, not Chunk (ADR-0011): a rendered statement is not a
    corpus row -- it has no single chunk_id until this function picks one (or
    several) from the projected relationship provenance, and Chunk has no field
    to hold that provenance or the `derived` flag a statement built on an
    ADR-0010 bridge needs to carry. See
    docs/adr/adr-0011-context-document-model.md.
    """
    raise NotImplementedError
