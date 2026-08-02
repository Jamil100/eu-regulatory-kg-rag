# kg-rag-eu-ai-act

Hybrid Knowledge Graph + Vector RAG over the **EU AI Act** and **GDPR**, built on the Cohere model stack (Command A, Command R7B, Embed v4, Rerank 3.5).

## Benchmark (fill after Phase 5)

| System | Single-hop | Two-hop | Three-hop | Cross-reg | Aggregation | Refusal | p95 latency | $/query |
|---|---|---|---|---|---|---|---|---|
| Vector-only | – | – | – | – | – | – | – | – |
| Vector + Rerank 3.5 | – | – | – | – | – | – | – | – |
| **Hybrid (graph+vector)** | – | – | – | – | – | – | – | – |

## 90-second demo

_TODO: embed demo video._

## Architecture

See [docs/kg-rag-eu-ai-act-roadmap.md](docs/kg-rag-eu-ai-act-roadmap.md) for the full design. High level:

```
Ingestion:  EUR-Lex → parser → chunker → {Command A extraction → Neo4j, Embed v4 → pgvector}
Query:      question → Command R7B router → {graph path | vector path} → Command A grounded answer + citations
```

The shared key is `chunk_id`, which lives in **both** stores.

## Honest failure notes

See [docs/failure-notes.md](docs/failure-notes.md).

## Setup

```bash
# 1. Environment
cp .env.example .env         # add your COHERE_API_KEY
uv sync                       # or: pip install -e .

# 2. Data stores
docker compose up -d          # neo4j + postgres/pgvector
# Docker inside WSL2 rather than Docker Desktop? Run compose from a WSL shell.
# WSL2 forwards Bolt to Windows on localhost:7687, but it stops the container
# when the last WSL session closes -- keep one open, or bring it back up.

# 3. Ingest
python -m src.ingest.chunker data/eu-ai-act.html   # -> chunks-ai-act.jsonl
python -m src.ingest.chunker data/gdpr.html        # -> chunks-gdpr.jsonl
python -m src.ingest.extract --all      # Command A extraction (~$24, 1.5-3h)
python -m src.ingest.audit              # corpus-scale integrity report
python -m src.ingest.entity_resolution --apply
python -m src.ingest.graph_writer --apply --verify   # -> Neo4j
python -m src.index.embedder --apply    # -> pgvector

# 4. Serve
uvicorn src.api.app:app --reload

# 5. Ask
curl -X POST localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "How does the AI Act define a deployer?"}'
```

## License / attribution

Corpus sourced from [EUR-Lex](https://eur-lex.europa.eu/) (© European Union, reusable with attribution).
