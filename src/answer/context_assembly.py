"""Context assembly: dedupe by chunk_id across graph + vector paths.

Each document is labeled [GRAPH] or [PASSAGE] in its metadata before being
passed to Command A via the documents parameter.
"""

from __future__ import annotations

from src.schemas import ContextDoc


def assemble(graph_docs: list[ContextDoc], passage_docs: list[ContextDoc]) -> list[dict]:
    """Merge and dedupe context documents, labeling their source path.

    Inputs are ContextDoc, not Chunk (ADR-0011) -- `graph_docs` in particular are
    path_to_prose's rendered statements, which are not corpus rows. The return
    type stays `list[dict]` on purpose: this is the boundary where a typed
    ContextDoc becomes an untyped document for Cohere's `documents` parameter,
    and that boundary should not be blurred by returning a Pydantic model on one
    side of a Cohere API call and a plain dict on the other.
    """
    raise NotImplementedError
