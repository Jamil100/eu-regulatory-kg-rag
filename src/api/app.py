"""FastAPI app.

POST /ask -> {answer, citations[], route, latency_ms, cost_usd} with
server-side validated citations.
"""

from __future__ import annotations

import time

from fastapi import FastAPI

from src.schemas import AskRequest, AskResponse

app = FastAPI(title="kg-rag-eu-ai-act")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    """Route -> retrieve -> assemble -> generate -> validate citations."""
    start = time.perf_counter()
    # TODO: router -> graph/vector paths -> context assembly -> generate -> validate
    raise NotImplementedError
