"""BGE-ZH embedding server — compatible with BGEZhEmbeddingProvider.

Expects the model at /models/bge-small-zh (mounted read-only).
Exposes POST /embed with {"inputs": ["text", ...]} returning a JSON
array of 512-dim float vectors.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from sentence_transformers import SentenceTransformer

MODEL_PATH = os.environ.get("BGE_MODEL_PATH", "/models/bge-small-zh")

if not Path(MODEL_PATH).is_dir():
    raise FileNotFoundError(f"model directory not found: {MODEL_PATH}")

model = SentenceTransformer(MODEL_PATH)
app = FastAPI(title="BGE-ZH Embedding")


@app.post("/embed")
async def embed(request: Request) -> list[list[float]]:
    body = await request.json()
    texts: list[str] = body["inputs"]
    embeddings = model.encode(texts, normalize_embeddings=True)
    return embeddings.tolist()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
