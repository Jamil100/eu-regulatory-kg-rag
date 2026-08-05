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
# The Neo4j driver, the Postgres pool, the Cohere client and the entity index
# are built once at startup, not per request. Startup does not fail on a store
# that is down -- GET /health reports which one, and /ask answers 503 naming it.
curl localhost:8000/health
# {"status":"ok","neo4j":"ok","postgres":"ok","cohere":"ok","index":"ok"}

# 5. Ask
curl -X POST localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "How does the AI Act define a deployer?"}'
```

```jsonc
{
  "answer": "The AI Act defines a deployer as a natural or legal person, public authority, agency or other body using an AI system under its authority except where the AI system is used in the course of a personal non-professional activity.",
  "citations": [
    {
      "chunk_id": "aia-art3-def4",
      "start": 35,
      "end": 227,
      "text": "natural or legal person, public authority, agency or other body using an AI system under its authority except where the AI system is used in the course of a personal non-professional activity.",
      "citation_label": "AIA Art. 3(4)",
      "source": "PASSAGE",       // or GRAPH, for a rendered graph statement
      "document_id": "d0"
    }
  ],
  "route": "vector",              // graph | vector | both, chosen deterministically
  "latency_ms": 1907.87,          // the whole handler, not the model call
  "cost_usd": 0.0052137           // null if the route used an unpriced component
}
```

`start`/`end` index the answer string, so `answer[start:end] == text` holds and
is checked server-side. Every cited `chunk_id` is validated against the set of
documents the model was given before the response is returned; on a failure the
answer is regenerated once with the defect named, then stands or fails loudly.

Per-route cost and latency over the 23-question eval set are in
[docs/metrics/answer-path.md](docs/metrics/answer-path.md) — median **$0.0067**
and **3.4 s** pooled p50, `both` costing roughly twice `vector`. Reproduce with:

```bash
python -m src.api.ask_eval --eval             # the table, from the committed artifact
python -m src.api.ask_eval --eval --refresh   # re-run all 23 live (~$0.18)
```

## License / attribution

Corpus sourced from [EUR-Lex](https://eur-lex.europa.eu/) (© European Union, reusable with attribution).
