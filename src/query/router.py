"""Command R7B query router -> enum {graph, vector, both}.

Few-shot prompt, emits one token. Logs every decision
(question, route, latency, outcome) for the Phase 5 benchmark.
"""

from __future__ import annotations

from typing import Literal

Route = Literal["graph", "vector", "both"]


def route(question: str) -> Route:
    """Classify a question into graph / vector / both."""
    raise NotImplementedError
