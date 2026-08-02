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
