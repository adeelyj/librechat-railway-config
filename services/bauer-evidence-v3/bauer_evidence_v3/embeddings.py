from __future__ import annotations

import math
from typing import Any

import httpx


class EmbeddingGatewayError(RuntimeError):
    pass


class OpenAICompatibleEmbeddingProvider:
    """Small synchronous adapter for release-pinned PostgreSQL retrieval."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int = 1_024,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url or not api_key or not model:
            raise ValueError("base_url, api_key, and model are required")
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=self.timeout_seconds)

    def embed(self, text: str) -> tuple[float, ...]:
        return self.embed_many((text,))[0]

    def embed_many(
        self,
        texts: tuple[str, ...] | list[str],
    ) -> tuple[tuple[float, ...], ...]:
        normalized = tuple(self._validate_input(text) for text in texts)
        if not normalized:
            return ()
        payload: dict[str, Any] = {
            "model": self.model,
            "input": list(normalized),
            "encoding_format": "float",
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = self.client.post(
            f"{self.base_url}/embeddings",
            headers=headers,
            json=payload,
            timeout=self.timeout_seconds,
        )
        try:
            response.raise_for_status()
            body = response.json()
            data = body["data"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise EmbeddingGatewayError(
                "embedding gateway returned an invalid response"
            ) from exc
        if not isinstance(data, list) or len(data) != len(normalized):
            raise EmbeddingGatewayError("embedding gateway returned the wrong result count")
        ordered: list[tuple[float, ...] | None] = [None] * len(normalized)
        for fallback_index, item in enumerate(data):
            if not isinstance(item, dict):
                raise EmbeddingGatewayError("embedding result must be an object")
            index = item.get("index", fallback_index)
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(ordered)
                or ordered[index] is not None
            ):
                raise EmbeddingGatewayError("embedding result index is invalid")
            ordered[index] = self._validate_vector(item.get("embedding"))
        if any(vector is None for vector in ordered):
            raise EmbeddingGatewayError("embedding results are incomplete")
        return tuple(vector for vector in ordered if vector is not None)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    @staticmethod
    def _validate_input(text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("embedding input must be text")
        normalized = text.strip()
        if not normalized:
            raise ValueError("embedding input must not be empty")
        if len(normalized) > 32_000:
            raise ValueError("embedding input exceeds the supported character limit")
        return normalized

    def _validate_vector(self, raw_vector: Any) -> tuple[float, ...]:
        if not isinstance(raw_vector, list) or len(raw_vector) != self.dimensions:
            raise EmbeddingGatewayError(
                f"embedding dimension mismatch: expected {self.dimensions}"
            )
        try:
            vector = tuple(float(value) for value in raw_vector)
        except (TypeError, ValueError) as exc:
            raise EmbeddingGatewayError("embedding contains a non-numeric value") from exc
        if not all(math.isfinite(value) for value in vector):
            raise EmbeddingGatewayError("embedding contains a non-finite value")
        return vector
