"""Central configuration loaded from environment (.env)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Both spellings are accepted because the extractor has always read CO_API_KEY
    # first. Resolving them in one place keeps `get_client()` and the embedder from
    # picking different keys when both variables are set.
    cohere_api_key: str = os.getenv("CO_API_KEY") or os.getenv("COHERE_API_KEY", "")
    cohere_api_key_var: str = "CO_API_KEY" if os.getenv("CO_API_KEY") else "COHERE_API_KEY"

    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "password")

    postgres_dsn: str = os.getenv("POSTGRES_DSN", "postgresql://kgrag:password@localhost:5432/kgrag")

    model_extract: str = os.getenv("MODEL_EXTRACT", "command-a-03-2025")
    model_router: str = os.getenv("MODEL_ROUTER", "command-r7b-12-2024")
    model_embed: str = os.getenv("MODEL_EMBED", "embed-v4.0")
    model_rerank: str = os.getenv("MODEL_RERANK", "rerank-v3.5")
    model_generate: str = os.getenv("MODEL_GENERATE", "command-a-03-2025")


settings = Settings()

# Cohere list prices, USD per token, keyed by the same model strings as the
# settings above -- a model swap and its price move together in one diff.
#
# command-a-03-2025 and embed-v4.0 duplicate the constants already in
# src/ingest/extract.py (PRICE_INPUT_PER_TOKEN=2.50/1M, PRICE_OUTPUT_PER_TOKEN=
# 10.00/1M) and src/index/embedder.py (PRICE_INPUT_PER_TOKEN=0.12/1M). Repeated
# rather than imported: those two modules price a batch ingestion job, this
# prices a live request, and a constant should not create a cross-layer import
# just to avoid typing four numbers twice.
#
# command-r7b-12-2024 is the roadmap's own citation (docs/kg-rag-eu-ai-act-
# roadmap.md, S3): "~$0.0375/$0.15 per 1M tokens" -- not independently priced
# here, so it inherits whatever staleness that citation has.
#
# rerank-v3.5 is deliberately None. As of 2026-08-02 Cohere's public pricing
# page quotes an hourly "Model Vault" rate for Rerank 3.5 ($5/hr) with no
# visible per-search or per-token figure, which does not translate into a
# marginal per-query cost. Guessing a number here would be indistinguishable
# from a measured one in `cost_usd` -- Step 4 of the Phase 3 plan must replace
# this with an actual measured bill before the vector+rerank route's cost is
# trustworthy. Until then, a request that reranks reports a `None` component,
# not a wrong number that looks confident.
PRICES: dict[str, dict[str, float | None]] = {
    "command-a-03-2025":   {"input": 2.50 / 1_000_000, "output": 10.00 / 1_000_000},
    "command-r7b-12-2024": {"input": 0.0375 / 1_000_000, "output": 0.15 / 1_000_000},
    "embed-v4.0":          {"input": 0.12 / 1_000_000, "output": None},
    "rerank-v3.5":         {"input": None, "output": None},
}


def price_of(model: str, input_tokens: int, output_tokens: int = 0) -> float | None:
    """USD cost of one call, or None if any needed rate is unpriced.

    None must propagate rather than be treated as zero -- a route that includes
    an unpriced call (today, any route that reranks) has an unknown total cost,
    and reporting it as a number that happens to be short by the rerank cost is
    worse than reporting that it is unknown.
    """
    rates = PRICES.get(model)
    if rates is None:
        return None
    input_rate, output_rate = rates["input"], rates["output"]
    if input_rate is None or (output_tokens and output_rate is None):
        return None
    return input_tokens * input_rate + output_tokens * (output_rate or 0.0)
