"""Programmatic access to the pgvector schema (see schema.sql).

Provides connect + ensure-schema helpers so the index can be created outside
of the docker init hook (e.g. against Azure Postgres).
"""

from __future__ import annotations

from pathlib import Path

SCHEMA_SQL = Path(__file__).parent / "schema.sql"


def ensure_schema() -> None:
    """Apply schema.sql against the configured Postgres DSN (idempotent)."""
    raise NotImplementedError
