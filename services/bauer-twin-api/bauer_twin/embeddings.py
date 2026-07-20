from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Iterable

import httpx

VECTOR_DIMENSIONS = 1024

_NORMALIZE = {
    "stickstoff": "nitrogen",
    "n2": "nitrogen",
    "atemluft": "breathing_air",
    "breathing": "breathing_air",
    "luft": "air",
    "verdichter": "compressor",
    "kompressor": "compressor",
    "filterpatrone": "cartridge",
    "drucksensor": "pressure_sensor",
    "taupunktsensor": "dewpoint_sensor",
    "steuerung": "controller",
    "speicher": "storage",
}


def deterministic_embedding(text: str) -> list[float]:
    """Feature-hashed fallback used only for local tests and degraded demo operation."""
    vector = [0.0] * VECTOR_DIMENSIONS
    tokens = re.findall(r"[a-z0-9-]+", text.lower())
    normalized = [_NORMALIZE.get(token, token) for token in tokens]
    for token in normalized:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % VECTOR_DIMENSIONS
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class EmbeddingClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("LOCALAI_EMBEDDING_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("LOCALAI_EMBEDDING_API_KEY", "")
        self.model = os.getenv("LOCALAI_EMBEDDING_MODEL", "local/embed-engineering")
        self.allow_fallback = os.getenv("ALLOW_FALLBACK_EMBEDDINGS", "true").lower() == "true"

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        items = list(texts)
        if not items:
            return []
        if not self.base_url or not self.api_key:
            if self.allow_fallback:
                return [deterministic_embedding(text) for text in items]
            raise RuntimeError("Local AI embedding endpoint credentials are required")

        url = self.base_url if self.base_url.endswith("/embeddings") else f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            with httpx.Client(timeout=180.0) as client:
                response = client.post(url, headers=headers, json={"model": self.model, "input": items})
                response.raise_for_status()
                payload = response.json()
            ordered = sorted(payload["data"], key=lambda item: item.get("index", 0))
            vectors = [item["embedding"] for item in ordered]
            if len(vectors) != len(items) or any(len(vector) != VECTOR_DIMENSIONS for vector in vectors):
                raise RuntimeError("Embedding endpoint returned an unexpected vector count or dimension")
            return vectors
        except Exception:
            if not self.allow_fallback:
                raise
            return [deterministic_embedding(text) for text in items]

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

