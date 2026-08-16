# kg-rag-eu-ai-act

Hybrid Knowledge Graph + Vector RAG over the **EU AI Act** and **GDPR**, built on the Cohere model stack (Command A, Command R7B, Embed v4, Rerank 3.5).

## Benchmark

**The hybrid did not beat the vector baselines. It is the slowest and most
expensive of the three and it wins no accuracy column outright.** That is the
result, and this section reports it rather than the result the repository was
built expecting.

Cells are `answers judged correct / rows scored`, over a 100-question stratified
eval set. Graded by an LLM judge (Command A, temperature 0) that agrees with a
hand-graded 20% sample **17 of 20 times (85%)**.

| System | Single-hop | Two-hop | Three-hop | Cross-reg | Aggregation | Refusal* | p95 latency | $/query |
|---|---|---|---|---|---|---|---|---|
| Vector-only | 16/20 | 9/20 | 0/13 | 3/15 | 0/9 | 11/20 | 9.8 s (n=30) | $0.0041 |
| Vector + Rerank 3.5 | 14/19 | 6/20 | 1/14 | 5/15 | 2/7 | 12/19 | 19.2 s (n=30) | $0.0065 |
| **Hybrid (graph+vector)** | 15/20 | 5/20 | 1/14 | 5/15 | 1/7 | 8/20 | 10.9 s (n=30) | $0.0076 |
| Hybrid, gold route [ceiling] | 15/19 | 4/20 | 2/14 | 4/14 | 1/7 | 7/19 | – | – |

`[ceiling]` is not a deployable system: it replays the eval set's hand-verified
route labels, which a live request does not have. **The gap to it is −1 answer**,
so the adopted router (70/99 on this set) is not what holds the hybrid back —
routing *more* questions to the graph did not help.

\*Refusal is three behaviours with three different correct outputs and is never
averaged into one number:

| System | Out-of-scope (cite nothing) | Unanswerable (cite nothing) | Hard-negative (must cite) |
|---|---|---|---|
| Vector-only | 2/5 | 4/5 | 5/10 |
| Vector + Rerank 3.5 | 4/5 | 3/5 | 5/9 |
| Hybrid (graph+vector) | 2/5 | 2/5 | 4/10 |
| Hybrid, gold route | 1/5 | 2/5 | 4/9 |

**The number that explains the table is not in it:** `partially_correct` is
42–46% of answers for *every* system. Only `correct` counts as a pass, so nearly
half the mass sits in one excluded bucket identically across all four arms, and
the differences between systems are swamped. On three-hop every system scores
exactly 8 partial / 5 wrong whether or not it received graph statements. These
systems mostly produce legally incomplete answers, and they do so at the same rate.

Three measured facts constrain how much of this the architecture can be blamed for:

- **22.7% of gold passages are unreachable** by the vector path at any `k ≤ 50` —
  but 0% on single-hop and 35% on aggregation. The retrieval ceiling predicted
  parity at the easy end and a collapse at the hard end, and that is what happened.
- **The adopted graph budget retains 20 of the 61 reachable gold chunks.**
  The hybrid enters the benchmark having already discarded two-thirds of what its
  own graph path found ([ADR-0014](docs/adr/adr-0014-graph-statement-budget.md)).
- **The three systems agree far more than they differ.** 40% / 43% / 36% correct.

Reproduce, with no API key and no containers:

```bash
python -m eval.run_benchmark --eval        # the table, from the committed artifact
python -m eval.judge --agreement           # the 85%, and the 3 rows it disagreed on
```

Full method, caveats and the debugging narrative:
[docs/metrics/benchmark.md](docs/metrics/benchmark.md) ·
[docs/failure-notes.md](docs/failure-notes.md)

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
