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
# rerank-v3.5 stays None *here* and is priced by `price_of_rerank()` below
# instead. Rerank is not billed per token at all -- it is billed per search unit
# -- so a token-shaped entry in this table could only ever be wrong. See the
# block above that function.
PRICES: dict[str, dict[str, float | None]] = {
    "command-a-03-2025":   {"input": 2.50 / 1_000_000, "output": 10.00 / 1_000_000},
    "command-r7b-12-2024": {"input": 0.0375 / 1_000_000, "output": 0.15 / 1_000_000},
    "embed-v4.0":          {"input": 0.12 / 1_000_000, "output": None},
    "rerank-v3.5":         {"input": None, "output": None},
}

# Rerank is billed per **search unit**, which `price_of()` cannot express and
# should not try to. Step 4 resolved the two halves separately, because they have
# very different confidence:
#
# THE QUANTITY IS MEASURED, NOT ESTIMATED. Cohere returns it on every response:
# `meta.billed_units.search_units` (verified live 2026-08-03 -- a 3-document
# search reported `search_units=1.0`). `src/query/reranker.py` records that
# figure per call and `eval/rerank-eval.jsonl` stores it, so the billed quantity
# behind every published number is auditable rather than reconstructed. This
# matters more than it looks: Cohere splits documents over 500 tokens into chunks
# that each count separately, and this corpus has chunks up to 864 tokens
# (`gdpr-art70-para1`), so a 50-document search does not reliably cost 1 unit.
# Reading the reported value sidesteps that rule entirely.
#
# THE RATE IS A THIRD-PARTY CITATION AND IS WEAKER THAN EVERY OTHER NUMBER HERE.
# Checked 2026-08-03: cohere.com/pricing publishes only a Model Vault hourly rate
# for Rerank 3.5 ($5/hr, $3,250/mo) and docs.cohere.com/docs/rerank-overview
# states no price at all. Cohere's own pricing page does define the unit -- "A
# single search unit is defined as one query with up to 100 documents to be
# ranked" -- but not what it costs. $2.00 per 1,000 searches is the figure
# third-party pricing aggregators carry for Rerank 3.5; it is the historical
# Rerank 3 API rate and no Cohere-owned page confirms it today.
#
# It is recorded rather than left None so `/ask` can report a cost at all, but it
# is the one rate in this file with no first-party source, and
# `docs/metrics/query-path.md` says so wherever a rerank cost appears. Because
# the artifact stores `search_units`, correcting this constant re-prices every
# historical measurement without re-running anything.
RERANK_PRICE_PER_SEARCH: float | None = 2.00 / 1_000
RERANK_PRICE_SOURCE = (
    "third-party aggregator listings for Rerank 3.5, checked 2026-08-03; "
    "not published on cohere.com/pricing, which shows only a $5/hr Model Vault rate"
)


def price_of_rerank(search_units: float) -> float | None:
    """USD cost of one or more rerank searches, or None if the rate is unknown.

    `search_units` should be the value Cohere reported on the response
    (`meta.billed_units.search_units`), not a count of documents or of calls.
    """
    if RERANK_PRICE_PER_SEARCH is None:
        return None
    return search_units * RERANK_PRICE_PER_SEARCH


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
