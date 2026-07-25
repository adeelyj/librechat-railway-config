from __future__ import annotations

import sys
import unittest
from pathlib import Path

import httpx


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.embeddings import (  # noqa: E402
    EmbeddingGatewayError,
    OpenAICompatibleEmbeddingProvider,
)


class EmbeddingTests(unittest.TestCase):
    def test_openai_compatible_request_and_vector(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/embeddings")
            self.assertEqual(request.headers["authorization"], "Bearer secret")
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.25, -0.5, 0.75]}]},
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleEmbeddingProvider(
                base_url="https://model.invalid/v1/",
                api_key="secret",
                model="embedding-model",
                dimensions=3,
                client=client,
            )
            self.assertEqual(provider.embed("BM 40"), (0.25, -0.5, 0.75))

    def test_batch_results_are_restored_to_input_order(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleEmbeddingProvider(
                base_url="https://model.invalid/v1",
                api_key="secret",
                model="embedding-model",
                dimensions=2,
                client=client,
            )
            self.assertEqual(
                provider.embed_many(("first", "second")),
                ((1.0, 0.0), (0.0, 1.0)),
            )

    def test_dimension_and_finite_values_fail_closed(self) -> None:
        responses = iter(
            (
                b'{"data":[{"embedding":[0.1]}]}',
                b'{"data":[{"embedding":[0.1,NaN]}]}',
            )
        )

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=next(responses),
                headers={"content-type": "application/json"},
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleEmbeddingProvider(
                base_url="https://model.invalid/v1",
                api_key="secret",
                model="embedding-model",
                dimensions=2,
                client=client,
            )
            with self.assertRaisesRegex(EmbeddingGatewayError, "dimension"):
                provider.embed("one")
            with self.assertRaisesRegex(EmbeddingGatewayError, "non-finite"):
                provider.embed("two")


if __name__ == "__main__":
    unittest.main()
