"""The Phase 3 price table.

`price_of` must return None rather than a wrong number whenever a rate is
missing -- rerank-v3.5 is priced None/None on purpose (see the comment in
src/config.py: Cohere's public pricing has moved to an hourly "Model Vault"
rate for Rerank 3.5 with no visible marginal per-query figure). A route that
reranks must report an unknown cost, not a confident one that is silently
short by the rerank component.
"""

from __future__ import annotations

from src.config import PRICES, price_of


def test_command_a_price_matches_the_ingestion_constant():
    """Duplicated from src/ingest/extract.py's PRICE_INPUT_PER_TOKEN /
    PRICE_OUTPUT_PER_TOKEN on purpose (ADR-0011 rationale) -- this pins the two
    copies to the same numbers so they cannot drift apart silently."""
    assert price_of("command-a-03-2025", 1_000_000, 0) == 2.50
    assert price_of("command-a-03-2025", 0, 1_000_000) == 10.00


def test_embed_v4_price_matches_the_ingestion_constant():
    assert price_of("embed-v4.0", 1_000_000, 0) == 0.12


def test_router_price_matches_the_roadmap_citation():
    assert price_of("command-r7b-12-2024", 1_000_000, 0) == 0.0375
    assert price_of("command-r7b-12-2024", 0, 1_000_000) == 0.15


def test_rerank_price_is_unknown_not_zero():
    """None must propagate, not silently become 0.0 -- see module docstring."""
    assert price_of("rerank-v3.5", 1_000_000, 1_000_000) is None
    assert PRICES["rerank-v3.5"]["input"] is None
    assert PRICES["rerank-v3.5"]["output"] is None


def test_unpriced_model_returns_none():
    assert price_of("some-future-model", 100) is None


def test_output_only_charged_when_output_tokens_given():
    """A model with no output rate should still price a pure-input call."""
    assert price_of("embed-v4.0", 1_000_000, 0) == 0.12
