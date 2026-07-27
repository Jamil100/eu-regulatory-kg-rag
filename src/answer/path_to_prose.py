"""Convert graph paths into readable statements before they reach the prompt.

e.g. (deployer)-[APPLIES_TO]-(FRIA obligation)-[IMPOSED_BY]-(AIA Art. 27)
  -> "Deployers of high-risk systems must conduct a fundamental rights impact
      assessment (AI Act, Article 27)."
Each statement keeps its source_chunk_id.
"""

from __future__ import annotations

from src.schemas import Chunk


def path_to_prose(paths: list[dict]) -> list[Chunk]:
    """Render Neo4j paths as prose statements carrying source_chunk_id."""
    raise NotImplementedError
